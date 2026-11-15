"""POST /measurements - validates a measurement and stores it (idempotent, FR2/FR3/FR4)."""
import json
import os
import time

from botocore.exceptions import ClientError

from common import db, http, logs, validation


def _parse_body(event):
    raw = event.get("body")
    if not raw:
        raise http.ApiError(400, "INVALID_JSON", "The request body is empty")
    try:
        return json.loads(raw)
    except ValueError:
        raise http.ApiError(400, "INVALID_JSON", "The request body is not valid JSON") from None


def _ingest(event):
    measurement = validation.validate_measurement(_parse_body(event))
    now = db.iso_now()

    # register / refresh the device; a deactivated device is refused
    try:
        db.table("DEVICES_TABLE").update_item(
            Key={"device_id": measurement["device_id"]},
            UpdateExpression=("SET last_seen = :now, last_timestamp = :ts, "
                              "registered_at = if_not_exists(registered_at, :now), "
                              "#st = if_not_exists(#st, :active), #src = if_not_exists(#src, :auto)"),
            ConditionExpression="attribute_not_exists(#st) OR #st <> :inactive",
            ExpressionAttributeNames={"#st": "status", "#src": "source"},
            ExpressionAttributeValues={":now": now, ":ts": measurement["timestamp"], ":active": "ACTIVE",
                                       ":inactive": "INACTIVE", ":auto": "auto"},
        )
    except ClientError as exc:
        if db.is_conditional_failure(exc):
            raise http.ApiError(403, "DEVICE_DISABLED",
                                f"Device '{measurement['device_id']}' is deactivated") from None
        raise

    ttl_days = int(os.environ.get("MEASUREMENT_TTL_DAYS", "7"))
    item = dict(measurement)
    item["received_at"] = now
    item[os.environ.get("TTL_ATTRIBUTE", "expires_at")] = int(time.time()) + ttl_days * 86400
    try:
        db.table("MEASUREMENTS_TABLE").put_item(
            Item=db.to_decimal(item), ConditionExpression="attribute_not_exists(device_id)")
    except ClientError as exc:
        if db.is_conditional_failure(exc):  # same device + same timestamp: ignored (FR4)
            logs.emit_metric("DuplicateMeasurements")
            return http.response(200, {"status": "duplicate", "device_id": measurement["device_id"],
                                       "timestamp": measurement["timestamp"]})
        raise

    logs.emit_metric("MeasurementsStored")
    return http.response(201, {"status": "stored", "device_id": measurement["device_id"],
                               "timestamp": measurement["timestamp"]})


def handler(event, context):
    started = time.perf_counter()
    try:
        return _ingest(event)
    except validation.ValidationError as exc:
        logs.emit_metric("ValidationErrors")
        logs.log("INFO", "measurement_rejected", field=exc.field, reason=str(exc))
        return http.error_response(400, "VALIDATION_ERROR", str(exc), exc.field)
    except http.ApiError as exc:
        return http.error_response(exc.status, exc.code, exc.message, exc.field)
    except Exception as exc:  # never a silent failure: log it, answer a unified 500
        logs.log("ERROR", "ingest_failed", error=repr(exc))
        logs.emit_metric("IngestFailures")
        return http.error_response(500, "INTERNAL_ERROR", "Internal server error")
    finally:
        logs.emit_metric("IngestDurationMs", (time.perf_counter() - started) * 1000, unit="Milliseconds")
