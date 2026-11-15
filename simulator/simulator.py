#!/usr/bin/env python3
"""
SensorWatch IoT simulator (module M1).

Generates measurements for N simulated sensors and sends them to the REST API
(POST /measurements, x-api-key). It can inject anomalies, low batteries and silent
sensors, and it records latency / throughput / error rate for the load tests (10, 100, 500).

The API URL and the API key are read from the CloudFormation stack outputs with boto3
(ApiUrl and SimulatorApiKeyId), unless --api-url / --api-key (or the environment variables
SENSORWATCH_API_URL / SENSORWATCH_API_KEY) are given.

Examples:
  python simulator.py --sensors 10  --duration 60 --interval 5 --anomaly-at 30 --anomaly-count 1
  python simulator.py --sensors 100 --duration 60 --interval 5 --silence-at 20 --silence-count 3
  python simulator.py --sensors 500 --duration 60 --interval 5 --workers 50
  python simulator.py --sensors 5 --duration 10 --interval 2 --dry-run
"""
import argparse
import json
import math
import os
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

DEFAULT_STACK = "SensoWatch-ComputeAPI"
_local = threading.local()


def iso_seconds():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_millis():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------- configuration (boto3)
def resolve_config(args):
    api_url = args.api_url or os.environ.get("SENSORWATCH_API_URL")
    api_key = args.api_key or os.environ.get("SENSORWATCH_API_KEY")
    if args.dry_run:
        return (api_url or "http://dry-run.local").rstrip("/"), api_key or "dry-run"
    if api_url and api_key:
        return api_url.rstrip("/"), api_key

    import boto3  # only needed when the values are not given explicitly

    session = boto3.Session(region_name=args.region)
    outputs = session.client("cloudformation").describe_stacks(StackName=args.stack)["Stacks"][0]["Outputs"]
    out = {o["OutputKey"]: o["OutputValue"] for o in outputs}
    api_url = api_url or out["ApiUrl"]
    if not api_key:
        key_id = out["SimulatorApiKeyId"]
        api_key = session.client("apigateway").get_api_key(apiKey=key_id, includeValue=True)["value"]
    return api_url.rstrip("/"), api_key


# ---------------------------------------------------------------- simulated sensor
class Sensor:
    def __init__(self, index, rng):
        self.rng = rng
        self.device_id = f"TH-B{index // 100 + 1}-{index % 100 + 1:03d}"
        self.temperature = rng.uniform(21.0, 25.0)
        self.humidity = rng.uniform(40.0, 55.0)
        self.battery = rng.uniform(70.0, 100.0)

    def measurement(self, timestamp, temperature=None, battery=None):
        # slow random walk so that normal data never triggers R1/R2/R3
        self.temperature = min(28.0, max(18.0, self.temperature + self.rng.uniform(-0.4, 0.4)))
        self.humidity = min(60.0, max(35.0, self.humidity + self.rng.uniform(-0.8, 0.8)))
        self.battery = max(20.0, self.battery - 0.01)
        return {
            "device_id": self.device_id,
            "temperature": round(self.temperature if temperature is None else temperature, 1),
            "humidity": round(self.humidity, 1),
            "battery": int(self.battery if battery is None else battery),
            "timestamp": timestamp,
        }


# ---------------------------------------------------------------- sending
def send(url, headers, payload, timeout, dry_run):
    if dry_run:
        return 201, 0.0, None
    session = getattr(_local, "session", None)
    if session is None:
        session = _local.session = requests.Session()
    start = time.perf_counter()
    try:
        response = session.post(url, json=payload, headers=headers, timeout=timeout)
        status, error = response.status_code, None
    except requests.RequestException as exc:
        status, error = 0, type(exc).__name__
    return status, (time.perf_counter() - start) * 1000.0, error


def percentile(sorted_values, p):
    if not sorted_values:
        return 0.0
    index = max(0, math.ceil(p / 100.0 * len(sorted_values)) - 1)
    return sorted_values[index]


# ---------------------------------------------------------------- main run
def run(args, api_url, api_key):
    rng = random.Random(args.seed)
    sensors = [Sensor(i, rng) for i in range(args.sensors)]

    order = list(range(args.sensors))
    rng.shuffle(order)
    wanted = args.anomaly_count + args.battery_count + args.silence_count
    if wanted > args.sensors:
        sys.exit("anomaly + battery + silence counts exceed the number of sensors")
    anomaly_ids = set(order[: args.anomaly_count])
    battery_ids = set(order[args.anomaly_count: args.anomaly_count + args.battery_count])
    silence_ids = set(order[args.anomaly_count + args.battery_count: wanted])

    url = f"{api_url}/measurements"
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    events, latencies, statuses, errors = [], [], {}, {}
    done = {"anomaly": False, "battery": False, "silence": False}
    silenced = set()
    late_rounds = 0
    sent = 0

    print(f"Simulating {args.sensors} sensors for {args.duration}s (every {args.interval}s) -> {url}")
    start = time.monotonic()
    started_at = iso_millis()
    round_no = 0
    previous_timestamp = None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= args.duration:
                break
            timestamp = iso_seconds()
            overrides = {}

            if args.anomaly_count and not done["anomaly"] and elapsed >= args.anomaly_at:
                done["anomaly"] = True
                for i in anomaly_ids:
                    overrides[i] = {"temperature": args.anomaly_value}
            if args.battery_count and not done["battery"] and elapsed >= args.battery_at:
                done["battery"] = True
                for i in battery_ids:
                    overrides.setdefault(i, {})["battery"] = 10
            if args.silence_count and not done["silence"] and elapsed >= args.silence_at:
                done["silence"] = True
                silenced = set(silence_ids)
                for i in silence_ids:
                    events.append({"type": "silence_start", "device_id": sensors[i].device_id,
                                   "from": iso_millis(), "last_measurement": previous_timestamp})

            jobs = []
            for i, sensor in enumerate(sensors):
                if i in silenced:
                    continue
                payload = sensor.measurement(timestamp, **overrides.get(i, {}))
                jobs.append((i, payload, pool.submit(send, url, headers, payload, args.timeout, args.dry_run)))

            for i, payload, future in jobs:
                status, latency, error = future.result()
                sent += 1
                latencies.append(latency)
                statuses[status] = statuses.get(status, 0) + 1
                if error:
                    errors[error] = errors.get(error, 0) + 1
                if i in overrides:
                    kind = "temperature_spike" if "temperature" in overrides[i] else "low_battery"
                    events.append({"type": kind, "device_id": payload["device_id"],
                                   "value": payload["temperature"] if kind == "temperature_spike" else payload["battery"],
                                   "measurement_timestamp": payload["timestamp"],
                                   "sent_at": iso_millis(), "http_status": status})
                if args.dry_run and round_no == 0 and i < 3:
                    print("  sample:", json.dumps(payload))

            previous_timestamp = timestamp
            round_no += 1
            next_tick = start + round_no * args.interval
            remaining = next_tick - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            else:
                late_rounds += 1

    total_time = time.monotonic() - start
    ok = sum(c for s, c in statuses.items() if 200 <= s < 300)
    failed = sent - ok
    ordered = sorted(latencies)
    summary = {
        "started_at": started_at,
        "sensors": args.sensors,
        "interval_s": args.interval,
        "duration_s": round(total_time, 1),
        "requests": sent,
        "success": ok,
        "errors": failed,
        "error_rate_percent": round(100.0 * failed / sent, 2) if sent else 0.0,
        "throughput_rps": round(sent / total_time, 2) if total_time else 0.0,
        "latency_ms": {
            "mean": round(statistics.fmean(ordered), 1) if ordered else 0.0,
            "p50": round(percentile(ordered, 50), 1),
            "p95": round(percentile(ordered, 95), 1),
            "max": round(ordered[-1], 1) if ordered else 0.0,
        },
        "status_codes": statuses,
        "exceptions": errors,
        "rounds_late": late_rounds,
        "injected_events": events,
    }
    return summary


def main():
    p = argparse.ArgumentParser(description="SensorWatch IoT simulator")
    p.add_argument("--sensors", type=int, default=10, help="number of simulated sensors (10, 100, 500...)")
    p.add_argument("--duration", type=float, default=60, help="test duration in seconds")
    p.add_argument("--interval", type=float, default=5, help="seconds between two measurements of a sensor (>= 1)")
    p.add_argument("--workers", type=int, default=20, help="parallel HTTP workers")
    p.add_argument("--timeout", type=float, default=10, help="HTTP timeout in seconds")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--anomaly-at", type=float, default=30, help="second at which the temperature spike is sent")
    p.add_argument("--anomaly-count", type=int, default=0, help="sensors that send the spike (0 = disabled)")
    p.add_argument("--anomaly-value", type=float, default=79.0, help="temperature of the spike")
    p.add_argument("--battery-at", type=float, default=30, help="second at which the low battery is sent")
    p.add_argument("--battery-count", type=int, default=0, help="sensors that send a low battery (0 = disabled)")
    p.add_argument("--silence-at", type=float, default=30, help="second at which sensors stop sending")
    p.add_argument("--silence-count", type=int, default=0, help="sensors that go silent (0 = disabled)")
    p.add_argument("--stack", default=DEFAULT_STACK, help="CloudFormation stack that exports ApiUrl / SimulatorApiKeyId")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--api-url", help="override the API base URL")
    p.add_argument("--api-key", help="override the API key value")
    p.add_argument("--report", default=None, help="JSON report path (default results/run-<timestamp>.json)")
    p.add_argument("--dry-run", action="store_true", help="generate payloads without calling AWS")
    args = p.parse_args()

    if args.interval < 1:
        sys.exit("--interval must be >= 1 (timestamps have a one-second resolution, duplicates are ignored)")
    api_url, api_key = resolve_config(args)
    summary = run(args, api_url, api_key)

    path = args.report or os.path.join("results", f"run-{args.sensors}-sensors-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)

    lat = summary["latency_ms"]
    print("\n===== RESULT =====")
    print(f"requests      : {summary['requests']} ({summary['success']} ok, {summary['errors']} errors, {summary['error_rate_percent']} %)")
    print(f"throughput    : {summary['throughput_rps']} req/s over {summary['duration_s']} s")
    print(f"latency (ms)  : mean {lat['mean']} | p50 {lat['p50']} | p95 {lat['p95']} | max {lat['max']}")
    print(f"status codes  : {summary['status_codes']}")
    if summary["exceptions"]:
        print(f"exceptions    : {summary['exceptions']}")
    if summary["rounds_late"]:
        print(f"WARNING       : {summary['rounds_late']} rounds took longer than the interval (increase --workers or --interval)")
    for event in summary["injected_events"]:
        print("injected      :", json.dumps(event))
    print(f"report saved  : {path}")


if __name__ == "__main__":
    main()
