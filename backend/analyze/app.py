
"""Anomaly detection (R1-R4), alert creation and device state update.

Supports:
- Direct asynchronous invocation from the ingest Lambda.
- DynamoDB Streams events.

A processing lock prevents concurrent or repeated analysis of
the same measurement.
"""
import json
import os
import time

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from common import config, db, ids, logs, rules


_deserializer = TypeDeserializer()

COOLDOWN_SECONDS = {
    "LOW_BATTERY": 3600
}

DEFAULT_COOLDOWN_SECONDS = int(
    os.environ.get("ALERT_COOLDOWN_SECONDS", "300")
)

ANALYSIS_LEASE_SECONDS = 900

# Analysis markers are purged by DynamoDB TTL (attribute expires_at) after 7 days.
ANALYSIS_MARKER_TTL_SECONDS = 7 * 86400

# ---------------------------------------------------------
# 1. Analysis idempotency lock
# ---------------------------------------------------------

def _claim_analysis(data, device_id, timestamp, request_id):
    """Claim a measurement for processing."""

    now = int(time.time())

    key = {
        "pk": f"ANALYSIS#{device_id}",
        "sk": timestamp
    }

    try:
        data.put_item(
            Item={
                **key,
                "status": "PROCESSING",
                "owner": request_id,
                "lease_until": now + ANALYSIS_LEASE_SECONDS,
                "expires_at": now + ANALYSIS_MARKER_TTL_SECONDS
            },
            ConditionExpression="attribute_not_exists(pk)"
        )

        return True

    except ClientError as exc:
        if not db.is_conditional_failure(exc):
            raise

    # An analysis marker already exists.
    existing = data.get_item(
        Key=key,
        ConsistentRead=True
    ).get("Item", {})

    if existing.get("status") == "COMPLETED":
        return False

    # Another invocation is still processing this measurement.
    if (
        existing.get("status") == "PROCESSING"
        and int(existing.get("lease_until", 0)) >= now
    ):
        return False

    # The previous processing attempt expired.
    try:
        data.update_item(
            Key=key,
            UpdateExpression=(
                "SET #s = :processing, "
                "#o = :owner, "
                "lease_until = :lease"
            ),
            ConditionExpression=(
                "(attribute_not_exists(#s) OR #s <> :completed) "
                "AND "
                "(attribute_not_exists(lease_until) OR lease_until < :now)"
            ),
            ExpressionAttributeNames={
                "#s": "status",
                "#o": "owner"
            },
            ExpressionAttributeValues={
                ":processing": "PROCESSING",
                ":completed": "COMPLETED",
                ":owner": request_id,
                ":lease": now + ANALYSIS_LEASE_SECONDS,
                ":now": now
            }
        )

        return True

    except ClientError as exc:
        if db.is_conditional_failure(exc):
            return False

        raise


def _complete_analysis(data, device_id, timestamp, request_id):
    """Mark a measurement as successfully analyzed."""

    data.update_item(
        Key={
            "pk": f"ANALYSIS#{device_id}",
            "sk": timestamp
        },
        UpdateExpression=(
            "SET #s = :completed, analyzed_at = :now"
        ),
        ConditionExpression=(
            "#s = :processing AND #o = :owner"
        ),
        ExpressionAttributeNames={
            "#s": "status",
            "#o": "owner"
        },
        ExpressionAttributeValues={
            ":completed": "COMPLETED",
            ":processing": "PROCESSING",
            ":owner": request_id,
            ":now": db.iso_now()
        }
    )


def _release_analysis(data, device_id, timestamp, request_id):
    """Release a processing lock after an error."""

    try:
        data.delete_item(
            Key={
                "pk": f"ANALYSIS#{device_id}",
                "sk": timestamp
            },
            ConditionExpression="#o = :owner",
            ExpressionAttributeNames={
                "#o": "owner"
            },
            ExpressionAttributeValues={
                ":owner": request_id
            }
        )

    except ClientError as exc:
        if not db.is_conditional_failure(exc):
            raise


# ---------------------------------------------------------
# 2. Alert creation
# ---------------------------------------------------------

def _create_alert(data, device_id, measurement, rule):

    alert_id = ids.alert_id(
        device_id,
        measurement["timestamp"],
        rule["type"],
        rule["metric"]
    )

    created_at = db.iso_now(millis=True)

    item = {
        "pk": f"ALERT#{alert_id}",
        "sk": "META",
        "alert_id": alert_id,
        "device_id": device_id,
        "type": rule["type"],
        "metric": rule["metric"],
        "value": rule["value"],
        "reason": rule["reason"],
        "severity": rule["severity"],
        "created_at": created_at,
        "status": "OPEN",
        "measurement_timestamp": measurement["timestamp"],
        "gsi1pk": "ALERT_STATUS#OPEN",
        "gsi1sk": created_at,
        "gsi2pk": f"DEVICE#{device_id}",
        "gsi2sk": created_at,
        "counts_for_state": True
    }

    try:
        data.put_item(
            Item=db.to_decimal(item),
            ConditionExpression="attribute_not_exists(pk)"
        )

    except ClientError as exc:
        if db.is_conditional_failure(exc):
            return item, False

        raise

    return item, True


# ---------------------------------------------------------
# 3. SNS notification
# ---------------------------------------------------------

def _notify(alert):

    try:
        db.client("sns").publish(
            TopicArn=os.environ["ADMIN_ALERTS_TOPIC_ARN"],

            Subject=(
                f"[SensorWatch] {alert['severity']} "
                f"{alert['type']} on {alert['device_id']}"
            )[:99],

            Message=json.dumps(
                db.public_item(alert),
                default=str
            ),

            MessageAttributes={
                "severity": {
                    "DataType": "String",
                    "StringValue": alert["severity"]
                },
                "device_id": {
                    "DataType": "String",
                    "StringValue": alert["device_id"]
                }
            }
        )

    except Exception as exc:
        # Alert is already stored.
        # Notification failure must not fail the analysis.
        logs.log(
            "WARNING",
            "alert_notification_failed",
            alert_id=alert["alert_id"],
            error=str(exc)
        )


# ---------------------------------------------------------
# 4. Main measurement processing
# ---------------------------------------------------------

def process_measurement(measurement, thresholds):

    device_id = measurement["device_id"]

    data = db.table("DATA_TABLE")

    window = max(
        1,
        int(thresholds["zscore-window"])
    )

    # Retrieve previous measurements for anomaly detection.
    history = db.table("MEASUREMENTS_TABLE").query(
        KeyConditionExpression=(
            Key("device_id").eq(device_id)
            & Key("timestamp").lt(measurement["timestamp"])
        ),
        ScanIndexForward=False,
        Limit=window
    ).get("Items", [])

    # Evaluate anomaly rules R1-R4.
    triggered = rules.evaluate(
        measurement,
        history,
        thresholds
    )

    # Retrieve current device state.
    state = data.get_item(
        Key={
            "pk": f"DEVICE#{device_id}",
            "sk": "STATE"
        }
    ).get("Item") or {}

    now_epoch = int(time.time())

    counted = []
    to_notify = []

    for rule in triggered:

        cooldown_key = (
            f"cd_{rule['type']}_{rule['metric']}"
        )

        cooldown = COOLDOWN_SECONDS.get(
            rule["type"],
            DEFAULT_COOLDOWN_SECONDS
        )

        # Avoid creating the same alert during cooldown.
        if (
            now_epoch - int(state.get(cooldown_key, 0))
            < cooldown
        ):
            continue

        alert, is_new = _create_alert(
            data,
            device_id,
            measurement,
            rule
        )

        counted.append(
            (cooldown_key, alert)
        )

        if is_new:
            to_notify.append(alert)


    # Update device state.
    names = {
        "#c": "connectivity"
    }

    values = {
        ":on": "ONLINE",
        ":d": device_id,
        ":n": len(counted),
        ":ls": (
            measurement.get("received_at")
            or db.iso_now()
        ),
        ":lv": db.to_decimal({
            "temperature": measurement["temperature"],
            "humidity": measurement["humidity"],
            "battery": measurement["battery"],
            "timestamp": measurement["timestamp"]
        })
    }

    assignments = [
        "#c = :on",
        "last_seen = :ls",
        "last_values = :lv",
        "device_id = :d"
    ]

    if counted:
        values[":now"] = now_epoch

    for index, (cooldown_key, _) in enumerate(counted):
        names[f"#cd{index}"] = cooldown_key
        assignments.append(
            f"#cd{index} = :now"
        )

    data.update_item(
        Key={
            "pk": f"DEVICE#{device_id}",
            "sk": "STATE"
        },
        UpdateExpression=(
            "SET "
            + ", ".join(assignments)
            + " REMOVE offline_since ADD open_alerts :n"
        ),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values
    )

    # Send notifications for newly created alerts.
    for alert in to_notify:
        _notify(alert)

    return len(to_notify)


# ---------------------------------------------------------
# 5. Lambda handler
# ---------------------------------------------------------

def handler(event, context):

    thresholds = config.load_thresholds()

    data = db.table("DATA_TABLE")

    analyzed = 0
    created = 0

    # Direct invocation from ingest Lambda.
    if event.get("source") == "sensorwatch.ingest":

        device_id = event["device_id"]
        timestamp = event["timestamp"]

        response = db.table("MEASUREMENTS_TABLE").get_item(
            Key={
                "device_id": device_id,
                "timestamp": timestamp
            }
        )

        measurement = response.get("Item")

        if not measurement:
            raise RuntimeError(
                f"Measurement not found: {device_id} / {timestamp}"
            )

        records = [measurement]

    # Compatibility with DynamoDB Streams.
    else:

        records = []

        for record in event.get("Records", []):

            if record.get("eventName") != "INSERT":
                continue

            image = record["dynamodb"]["NewImage"]

            measurement = {
                key: _deserializer.deserialize(value)
                for key, value in image.items()
            }

            records.append(measurement)

    # Process each measurement.
    for measurement in records:

        device_id = measurement["device_id"]
        timestamp = measurement["timestamp"]

        request_id = getattr(context, "aws_request_id", None) or ids.alert_id(
            device_id, timestamp, "REQUEST", str(time.time())
        )


        claimed = _claim_analysis(
            data,
            device_id,
            timestamp,
            request_id
        )

        if not claimed:

            logs.log(
                "INFO",
                "measurement_already_processing_or_analyzed",
                device_id=device_id,
                timestamp=timestamp
            )

            continue

        try:

            created += process_measurement(
                measurement,
                thresholds
            )

            _complete_analysis(
                data,
                device_id,
                timestamp,
                request_id
            )

            analyzed += 1

        except Exception:

            _release_analysis(
                data,
                device_id,
                timestamp,
                request_id
            )

            # Let Lambda retry the failed invocation.
            raise

    # Metrics and logs.
    logs.emit_metric(
        "MeasurementsAnalyzed",
        analyzed
    )

    logs.emit_metric(
        "AlertsCreated",
        created
    )

    logs.log(
        "INFO",
        "batch_analyzed",
        analyzed=analyzed,
        alerts=created
    )

    return {
        "analyzed": analyzed,
        "alerts": created
    }
