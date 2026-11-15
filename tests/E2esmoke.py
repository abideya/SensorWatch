#!/usr/bin/env python3
"""
End-to-end smoke test of the DEPLOYED SensorWatch platform.

It creates a test device (E2E-xxxxxx), goes through the API like the simulator and the administrator do,
and prints PASS / FAIL for each check, including the O2 objective (anomaly detected in < 20 s).

  python tests/e2e_smoke.py --password "<admin password>"

The API URL / key are read from the CloudFormation outputs with boto3 (like the simulator), or given with
--api-url / --api-key. Requirements: pip install requests boto3.
"""
import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import requests

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
    return ok


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def resolve(args):
    if args.api_url and args.api_key:
        return args.api_url.rstrip("/"), args.api_key
    import boto3
    session = boto3.Session(region_name=args.region)
    outputs = session.client("cloudformation").describe_stacks(StackName=args.stack)["Stacks"][0]["Outputs"]
    out = {o["OutputKey"]: o["OutputValue"] for o in outputs}
    key = args.api_key or session.client("apigateway").get_api_key(
        apiKey=out["SimulatorApiKeyId"], includeValue=True)["value"]
    return (args.api_url or out["ApiUrl"]).rstrip("/"), key


def wait_for(function, timeout, interval=1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = function()
        if value:
            return value
        time.sleep(interval)
    return None


def main():
    parser = argparse.ArgumentParser(description="SensorWatch end-to-end smoke test")
    parser.add_argument("--password", default=os.environ.get("SENSORWATCH_ADMIN_PASSWORD"), required=False)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--stack", default="SensoWatch-ComputeAPI")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--api-url")
    parser.add_argument("--api-key")
    parser.add_argument("--wait", type=float, default=30, help="seconds to wait for asynchronous results")
    args = parser.parse_args()
    if not args.password:
        sys.exit("Give the admin password with --password (or SENSORWATCH_ADMIN_PASSWORD)")

    api, key = resolve(args)
    device = "E2E-" + uuid.uuid4().hex[:6].upper()
    print(f"API: {api}\nTest device: {device}\n")

    def call(method, path, body=None, token=None, api_key=None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if api_key:
            headers["x-api-key"] = api_key
        response = requests.request(method, api + path, json=body, headers=headers, timeout=15)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    now = datetime.now(timezone.utc)

    def measurement(offset_seconds, temperature, at=None, **changes):
        data = {"device_id": device, "temperature": temperature, "humidity": 48.0, "battery": 87,
                "timestamp": iso((at or now) + timedelta(seconds=offset_seconds))}
        data.update(changes)
        return data

    # ---------------------------------------------------------------- security (FR12)
    status, _ = call("POST", "/measurements", measurement(-40, 22.0))
    check("POST /measurements without API key is refused (403)", status == 403, f"got {status}")

    status, _ = call("POST", "/auth/login", {"user_id": args.user, "password": "wrong-password"})
    check("login with a wrong password is a 401", status == 401, f"got {status}")

    status, body = call("POST", "/auth/login", {"user_id": args.user, "password": args.password})
    token = body.get("token") if status == 200 and isinstance(body, dict) else None
    if not check("login as ADMIN returns a token", bool(token), f"got {status}"):
        sys.exit("Cannot continue without a token (create the user with backend/scripts/create_admin.py)")

    status, _ = call("GET", "/devices")
    check("GET /devices without token is a 401", status == 401, f"got {status}")
    status, _ = call("GET", "/devices", token="not.a.token")
    check("GET /devices with an invalid token is a 401", status == 401, f"got {status}")

    # ---------------------------------------------------------------- ingestion (FR2, FR3, FR4)
    first = measurement(-40, 22.0)
    status, body = call("POST", "/measurements", first, api_key=key)
    check("valid measurement is stored (201)", status == 201, f"got {status} {body}")
    status, body = call("POST", "/measurements", first, api_key=key)
    check("same device + same timestamp is ignored (200 duplicate)",
          status == 200 and isinstance(body, dict) and body.get("status") == "duplicate", f"got {status} {body}")
    status, body = call("POST", "/measurements", measurement(-39, 22.0, humidity=150), api_key=key)
    check("invalid humidity is rejected with the field name (400)",
          status == 400 and body.get("error", {}).get("field") == "humidity", f"got {status} {body}")
    status, body = call("POST", "/measurements", {"device_id": device}, api_key=key)
    check("missing fields are rejected (400)", status == 400, f"got {status}")

    # ---------------------------------------------------------------- device state (FR1, FR10)
    def device_state(expected):
        code, data = call("GET", f"/devices/{device}", token=token)
        return data if code == 200 and data.get("state") == expected else None

    found = wait_for(lambda: device_state("ONLINE"), args.wait)
    check("device is auto-registered and ONLINE after its first measurement", found is not None)

    # ---------------------------------------------------------------- anomaly detection (FR5, FR6, O2)
    sent_at = datetime.now(timezone.utc)
    spike = measurement(0, 79.0, at=sent_at)  # timestamp taken at send time, so the O2 delay is not inflated
    status, _ = call("POST", "/measurements", spike, api_key=key)
    check("anomalous measurement (79 C) is accepted (201)", status == 201, f"got {status}")

    def threshold_alert():
        code, data = call("GET", f"/alerts?device_id={device}&status=OPEN", token=token)
        if code != 200:
            return None
        return next((a for a in data["items"] if a["type"] == "THRESHOLD"), None)

    alert = wait_for(threshold_alert, args.wait, interval=1)
    seen_after = (datetime.now(timezone.utc) - sent_at).total_seconds()
    if check("a THRESHOLD alert is created", alert is not None):
        check("alert is structured (device, value, reason, severity, status)",
              alert["device_id"] == device and float(alert["value"]) == 79.0 and alert["severity"] == "HIGH"
              and alert["status"] == "OPEN" and "79.0" in alert["reason"], str(alert))
        delay = (parse(alert["created_at"]) - parse(spike["timestamp"])).total_seconds()
        check("O2: alert created in under 20 s", delay < 20, f"{delay:.1f}s after the measurement timestamp "
              f"({seen_after:.1f}s until visible through the API)")
    check("device state becomes ALERT", wait_for(lambda: device_state("ALERT"), 5) is not None)

    # ---------------------------------------------------------------- alerts (FR6)
    code, data = call("GET", f"/alerts?device_id={device}&status=OPEN", token=token)
    open_alerts = data["items"] if code == 200 else []
    acknowledged = [call("POST", f"/alerts/{a['alert_id']}/ack", token=token)[0] for a in open_alerts]
    check("every open alert can be acknowledged (200)", bool(acknowledged) and all(s == 200 for s in acknowledged),
          f"{acknowledged}")
    if open_alerts:
        status, _ = call("POST", f"/alerts/{open_alerts[0]['alert_id']}/ack", token=token)
        check("acknowledging twice is a 409", status == 409, f"got {status}")
    check("device is back to ONLINE once its alerts are acknowledged",
          wait_for(lambda: device_state("ONLINE"), 5) is not None)

    # ---------------------------------------------------------------- history and statistics (FR8, FR9)
    status, body = call("GET", f"/devices/{device}/measurements?hours=1", token=token)
    temperatures = [float(i["temperature"]) for i in body["items"]] if status == 200 else []
    check("history returns the stored measurements in chronological order",
          status == 200 and temperatures == [22.0, 79.0], f"got {status} {temperatures}")
    status, body = call("GET", f"/devices/{device}/measurements?from=yesterday", token=token)
    check("a bad period is rejected (400)", status == 400, f"got {status}")
    status, body = call("GET", f"/devices/{device}/stats?hours=1", token=token)
    check("device statistics (avg / min / max / anomalies)",
          status == 200 and body["temperature"]["max"] == 79.0 and body["temperature"]["min"] == 22.0
          and body["anomalies"] >= 1, f"got {status} {body}")
    status, body = call("GET", "/stats", token=token)
    check("fleet statistics", status == 200 and body["devices_total"] >= 1 and "states" in body, f"got {status}")
    status, body = call("GET", "/devices?state=ONLINE", token=token)
    check("device list can be filtered by state",
          status == 200 and any(d["device_id"] == device for d in body["items"]), f"got {status}")

    # ---------------------------------------------------------------- device lifecycle (FR1)
    status, body = call("PATCH", f"/devices/{device}", {"status": "INACTIVE", "location": "E2E room"}, token=token)
    check("device can be deactivated", status == 200 and body["status"] == "INACTIVE", f"got {status}")
    status, body = call("POST", "/measurements", measurement(60, 22.5, at=sent_at), api_key=key)
    check("a deactivated device is refused (403)", status == 403, f"got {status}")
    status, body = call("PATCH", f"/devices/{device}", {"status": "ACTIVE"}, token=token)
    check("device can be reactivated", status == 200 and body["status"] == "ACTIVE", f"got {status}")
    status, _ = call("POST", "/measurements", measurement(61, 22.5, at=sent_at), api_key=key)
    check("an active device is accepted again", status == 201, f"got {status}")
    status, body = call("POST", "/devices", {"device_id": device, "location": "L", "type": "TH"}, token=token)
    check("registering an existing device is a 409", status == 409, f"got {status}")

    status, body = call("GET", "/does/not/exist", token=token)
    check("unknown route is a 404 with the unified error format",
          status == 404 and body.get("error", {}).get("code") == "NOT_FOUND", f"got {status}")

    print(f"\n{sum(results)}/{len(results)} checks passed")
    print(f"(test data of {device} is purged by the 7-day TTL; the device record can stay)")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()