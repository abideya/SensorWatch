"""EventBridge (every 5 minutes) -> marks silent devices OFFLINE and raises an alert (R5 / FR7)."""
import json
import os
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from common import config, db, ids, logs


def _reference(device):
    return device.get("last_seen") or device.get("registered_at") or device.get("created_at")


def _mark_offline(data, device, reference, silent_minutes, limit_minutes):
    device_id = device["device_id"]
    now = db.iso_now()
    try:  # only the first run that sees the device silent flips it (idempotent)
        data.update_item(
            Key={"pk": f"DEVICE#{device_id}", "sk": "STATE"},
            UpdateExpression="SET connectivity = :off, offline_since = :now, device_id = :d, last_seen = :ls",
            ConditionExpression="attribute_not_exists(connectivity) OR connectivity <> :off",
            ExpressionAttributeValues={":off": "OFFLINE", ":now": now, ":d": device_id, ":ls": reference},
        )
    except ClientError as exc:
        if db.is_conditional_failure(exc):
            return None
        raise

    alert_id = ids.alert_id(device_id, reference, "DEVICE_OFFLINE", "heartbeat")
    created_at = db.iso_now(millis=True)
    alert = {
        "pk": f"ALERT#{alert_id}", "sk": "META", "alert_id": alert_id, "device_id": device_id,
        "type": "DEVICE_OFFLINE", "metric": "heartbeat", "value": round(silent_minutes, 1),
        "reason": f"No measurement received for {silent_minutes:.0f} minutes (limit {limit_minutes:g})",
        "severity": "HIGH", "created_at": created_at, "status": "OPEN", "last_seen": reference,
        "gsi1pk": "ALERT_STATUS#OPEN", "gsi1sk": created_at,
        "gsi2pk": f"DEVICE#{device_id}", "gsi2sk": created_at,
        "counts_for_state": False,  # the OFFLINE state already comes from `connectivity`
    }
    try:
        data.put_item(Item=db.to_decimal(alert), ConditionExpression="attribute_not_exists(pk)")
    except ClientError as exc:
        if not db.is_conditional_failure(exc):
            raise
        return None
    try:
        db.client("sns").publish(
            TopicArn=os.environ["ADMIN_ALERTS_TOPIC_ARN"],
            Subject=f"[SensorWatch] HIGH DEVICE_OFFLINE on {device_id}"[:99],
            Message=json.dumps(db.public_item(alert), default=str),
            MessageAttributes={"severity": {"DataType": "String", "StringValue": "HIGH"},
                               "device_id": {"DataType": "String", "StringValue": device_id}},
        )
    except Exception as exc:
        logs.log("WARNING", "offline_notification_failed", device_id=device_id, error=str(exc))
    return alert


def handler(event, context):
    limit_minutes = config.load_thresholds()["offline-after-minutes"]
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=limit_minutes)
    data = db.table("DATA_TABLE")

    devices = db.scan_all(
        db.table("DEVICES_TABLE"),
        ProjectionExpression="device_id, #st, last_seen, registered_at, created_at",
        ExpressionAttributeNames={"#st": "status"},
    )
    silent = []
    for device in devices:
        if device.get("status") == "INACTIVE":
            continue
        reference = _reference(device)
        if reference and db.parse_iso(reference) < cutoff:
            silent.append((device, reference))

    marked = 0
    states = db.get_states([device["device_id"] for device, _ in silent])
    for device, reference in silent:
        if states.get(device["device_id"], {}).get("connectivity") == "OFFLINE":
            continue
        minutes = (datetime.now(timezone.utc) - db.parse_iso(reference)).total_seconds() / 60
        if _mark_offline(data, device, reference, minutes, limit_minutes):
            marked += 1

    logs.emit_metric("DevicesMarkedOffline", marked)
    logs.log("INFO", "offline_check_done", devices=len(devices), silent=len(silent), marked=marked)
    return {"devices": len(devices), "silent": len(silent), "marked_offline": marked}
