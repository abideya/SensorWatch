"""DynamoDB Streams -> anomaly detection (R1-R4), alert creation and device state update (FR5/FR6).

Any exception is re-raised on purpose: the stream mapping retries, bisects the batch and finally sends the
failed batch to the SQS error queue, so that no analysis error is lost silently.
"""
import json
import os
import time

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from common import config, db, ids, logs, rules

_deserializer = TypeDeserializer()
COOLDOWN_SECONDS = {"LOW_BATTERY": 3600}
DEFAULT_COOLDOWN_SECONDS = int(os.environ.get("ALERT_COOLDOWN_SECONDS", "300"))


def _create_alert(data, device_id, measurement, rule):
    alert_id = ids.alert_id(device_id, measurement["timestamp"], rule["type"], rule["metric"])
    created_at = db.iso_now(millis=True)
    item = {
        "pk": f"ALERT#{alert_id}", "sk": "META",
        "alert_id": alert_id, "device_id": device_id, "type": rule["type"], "metric": rule["metric"],
        "value": rule["value"], "reason": rule["reason"], "severity": rule["severity"],
        "created_at": created_at, "status": "OPEN", "measurement_timestamp": measurement["timestamp"],
        "gsi1pk": "ALERT_STATUS#OPEN", "gsi1sk": created_at,
        "gsi2pk": f"DEVICE#{device_id}", "gsi2sk": created_at,
        "counts_for_state": True,
    }
    try:
        data.put_item(Item=db.to_decimal(item), ConditionExpression="attribute_not_exists(pk)")
    except ClientError as exc:
        if db.is_conditional_failure(exc):
            return item, False  # already created by an earlier attempt of the same measurement
        raise
    return item, True


def _notify(alert):
    try:
        db.client("sns").publish(
            TopicArn=os.environ["ADMIN_ALERTS_TOPIC_ARN"],
            Subject=f"[SensorWatch] {alert['severity']} {alert['type']} on {alert['device_id']}"[:99],
            Message=json.dumps(db.public_item(alert), default=str),
            MessageAttributes={
                "severity": {"DataType": "String", "StringValue": alert["severity"]},
                "device_id": {"DataType": "String", "StringValue": alert["device_id"]},
            },
        )
    except Exception as exc:  # the alert is stored: a notification problem must not fail the analysis
        logs.log("WARNING", "alert_notification_failed", alert_id=alert["alert_id"], error=str(exc))


def process_measurement(measurement, thresholds):
    device_id = measurement["device_id"]
    data = db.table("DATA_TABLE")
    window = max(1, int(thresholds["zscore-window"]))

    history = db.table("MEASUREMENTS_TABLE").query(
        KeyConditionExpression=Key("device_id").eq(device_id) & Key("timestamp").lt(measurement["timestamp"]),
        ScanIndexForward=False, Limit=window,
    ).get("Items", [])
    triggered = rules.evaluate(measurement, history, thresholds)

    state = data.get_item(Key={"pk": f"DEVICE#{device_id}", "sk": "STATE"}).get("Item") or {}
    now_epoch = int(time.time())
    counted, to_notify = [], []
    for rule in triggered:
        cooldown_key = f"cd_{rule['type']}_{rule['metric']}"
        cooldown = COOLDOWN_SECONDS.get(rule["type"], DEFAULT_COOLDOWN_SECONDS)
        if now_epoch - int(state.get(cooldown_key, 0)) < cooldown:
            continue  # same alert already raised recently (duplicate prevention)
        alert, is_new = _create_alert(data, device_id, measurement, rule)
        counted.append((cooldown_key, alert))
        if is_new:
            to_notify.append(alert)

    names, values = {"#c": "connectivity"}, {
        ":on": "ONLINE", ":d": device_id, ":n": len(counted), ":now": now_epoch,
        ":ls": measurement.get("received_at") or db.iso_now(),
        ":lv": db.to_decimal({"temperature": measurement["temperature"], "humidity": measurement["humidity"],
                              "battery": measurement["battery"], "timestamp": measurement["timestamp"]}),
    }
    assignments = ["#c = :on", "last_seen = :ls", "last_values = :lv", "device_id = :d"]
    for index, (cooldown_key, _) in enumerate(counted):
        names[f"#cd{index}"] = cooldown_key
        assignments.append(f"#cd{index} = :now")
    data.update_item(
        Key={"pk": f"DEVICE#{device_id}", "sk": "STATE"},
        UpdateExpression="SET " + ", ".join(assignments) + " REMOVE offline_since ADD open_alerts :n",
        ExpressionAttributeNames=names, ExpressionAttributeValues=values,
    )

    for alert in to_notify:
        _notify(alert)
    return len(to_notify)


def handler(event, context):
    thresholds = config.load_thresholds()
    analyzed = created = 0
    for record in event.get("Records", []):
        if record.get("eventName") != "INSERT":
            continue
        image = record["dynamodb"]["NewImage"]
        measurement = {key: _deserializer.deserialize(value) for key, value in image.items()}
        created += process_measurement(measurement, thresholds)
        analyzed += 1
    logs.emit_metric("MeasurementsAnalyzed", analyzed)
    logs.emit_metric("AlertsCreated", created)
    logs.log("INFO", "batch_analyzed", analyzed=analyzed, alerts=created)
    return {"analyzed": analyzed, "alerts": created}
