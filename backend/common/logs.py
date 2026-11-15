"""Structured JSON logs and CloudWatch custom metrics (Embedded Metric Format)."""
import json
import time

NAMESPACE = "SensorWatch"


def log(level, event, **fields):
    print(json.dumps({"level": level, "event": event, **fields}, default=str))


def emit_metric(name, value=1, unit="Count", **dimensions):
    """Writes one metric as a log line: CloudWatch turns it into a custom metric (no API call needed)."""
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": [sorted(dimensions)] if dimensions else [[]],
                "Metrics": [{"Name": name, "Unit": unit}],
            }],
        },
        name: float(value),
        **dimensions,
    }
    print(json.dumps(record, default=str))
