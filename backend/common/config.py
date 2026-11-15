"""Detection thresholds: read from SSM Parameter Store (cached), with defaults if SSM is unreachable."""
import os
import time

from common import db, logs

DEFAULTS = {
    "temperature-max": 45.0,
    "temperature-min": 0.0,
    "humidity-max": 80.0,
    "humidity-min": 10.0,
    "temperature-delta-max": 10.0,
    "zscore-threshold": 3.0,
    "zscore-window": 20.0,
    "zscore-min-samples": 10.0,
    "battery-min": 15.0,
    "offline-after-minutes": 10.0,
}

_cache = {"at": 0.0, "values": None}


def load_thresholds(ttl=60, ssm=None):
    now = time.time()
    if _cache["values"] is not None and now - _cache["at"] < ttl:
        return _cache["values"]

    values = dict(DEFAULTS)
    path = os.environ.get("THRESHOLDS_PREFIX", "/SensorWatch/thresholds/").rstrip("/")
    try:
        client = ssm or db.client("ssm")
        for page in client.get_paginator("get_parameters_by_path").paginate(Path=path):
            for parameter in page.get("Parameters", []):
                key = parameter["Name"].rsplit("/", 1)[-1]
                try:
                    values[key] = float(parameter["Value"])
                except ValueError:
                    logs.log("WARNING", "bad_threshold", name=parameter["Name"], value=parameter["Value"])
    except Exception as exc:  # SSM unreachable: keep working with the last known / default values
        logs.log("WARNING", "ssm_unavailable", error=str(exc))
        if _cache["values"] is not None:
            return _cache["values"]
    _cache.update(at=now, values=values)
    return values


def reset_cache():
    _cache.update(at=0.0, values=None)
