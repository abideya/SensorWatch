"""Input validation (FR3): every rejection carries a precise message and the offending field."""
import math
import re
from datetime import datetime, timedelta, timezone

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# physical sanity limits (a 79 C reading is valid data and must reach the anomaly detection)
LIMITS = {"temperature": (-50.0, 150.0), "humidity": (0.0, 100.0), "battery": (0.0, 100.0)}


class ValidationError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


def validate_device_id(value, field="device_id"):
    if not isinstance(value, str) or not DEVICE_ID_RE.match(value):
        raise ValidationError(
            field, f"'{field}' must be 1-64 characters (letters, digits, '_', '-' or '.'), got {value!r}")
    return value


def parse_timestamp(value, field="timestamp"):
    if not isinstance(value, str) or not TIMESTAMP_RE.match(value):
        raise ValidationError(
            field, f"'{field}' must be an ISO-8601 UTC string like 2026-10-05T09:15:00Z, got {value!r}")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValidationError(field, f"'{field}' is not a valid date, got {value!r}") from None


def _number(payload, field):
    if field not in payload:
        raise ValidationError(field, f"Missing required field '{field}'")
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(field, f"'{field}' must be a number, got {value!r}")
    low, high = LIMITS[field]
    if not low <= value <= high:
        raise ValidationError(field, f"'{field}' must be between {low:g} and {high:g}, got {value}")
    return value


def validate_measurement(payload, now=None, max_future_seconds=300):
    if not isinstance(payload, dict):
        raise ValidationError("body", "The request body must be a JSON object")
    if "device_id" not in payload:
        raise ValidationError("device_id", "Missing required field 'device_id'")
    device_id = validate_device_id(payload["device_id"])
    if "timestamp" not in payload:
        raise ValidationError("timestamp", "Missing required field 'timestamp'")
    timestamp = payload["timestamp"]
    moment = parse_timestamp(timestamp)
    now = now or datetime.now(timezone.utc)
    if moment > now + timedelta(seconds=max_future_seconds):
        raise ValidationError("timestamp", f"'timestamp' is in the future ({timestamp})")

    measurement = {
        "device_id": device_id,
        "temperature": _number(payload, "temperature"),
        "humidity": _number(payload, "humidity"),
        "battery": _number(payload, "battery"),
        "timestamp": timestamp,
    }
    if "seq" in payload:
        seq = payload["seq"]
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise ValidationError("seq", f"'seq' must be a non-negative integer, got {seq!r}")
        measurement["seq"] = seq
    return measurement
