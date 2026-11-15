"""EventBridge (daily) -> per-device and fleet statistics of a day, exported to S3 (FR8/FR11).

Without {"day": "YYYY-MM-DD"} in the event, the previous UTC day is processed.
"""
import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key

from common import db, logs, stats, validation

METRICS = ("temperature", "humidity", "battery")


def _device_stats(measurements_table, data_table, device_id, start, end):
    items = db.query_all(
        measurements_table,
        KeyConditionExpression=Key("device_id").eq(device_id) & Key("timestamp").between(start, end),
        ProjectionExpression="temperature, humidity, battery",
    )
    low, high = db.alert_bounds(start, end)
    anomalies = db.query_count(
        data_table, IndexName="gsi2",
        KeyConditionExpression=Key("gsi2pk").eq(f"DEVICE#{device_id}") & Key("gsi2sk").between(low, high))
    return {metric: stats.summarize([i[metric] for i in items]) for metric in METRICS}, len(items), anomalies


def _fleet(rows, devices_total, active_devices):
    fleet = {"devices_total": devices_total, "active_devices": active_devices,
             "devices_reporting": sum(1 for r in rows if r["measurements"]),
             "measurements": sum(r["measurements"] for r in rows),
             "anomalies": sum(r["anomalies"] for r in rows)}
    for metric in METRICS:
        parts = [r[metric] for r in rows if r[metric]["count"]]
        total = sum(p["count"] for p in parts)
        fleet[metric] = {
            "count": total,
            "avg": round(sum(p["avg"] * p["count"] for p in parts) / total, 2) if total else None,
            "min": min((p["min"] for p in parts), default=None),
            "max": max((p["max"] for p in parts), default=None),
        }
    return fleet


def _csv(rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["device_id", "measurements", "anomalies"] +
                    [f"{m}_{k}" for m in METRICS for k in ("avg", "min", "max")])
    for row in rows:
        writer.writerow([row["device_id"], row["measurements"], row["anomalies"]] +
                        [row[m][k] for m in METRICS for k in ("avg", "min", "max")])
    return out.getvalue()


def handler(event, context):
    day = (event or {}).get("day") or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    validation.parse_timestamp(f"{day}T00:00:00Z", "day")
    start, end = f"{day}T00:00:00Z", f"{day}T23:59:59Z"

    measurements_table, data_table = db.table("MEASUREMENTS_TABLE"), db.table("DATA_TABLE")
    devices = db.scan_all(db.table("DEVICES_TABLE"), ProjectionExpression="device_id, #st",
                          ExpressionAttributeNames={"#st": "status"})
    rows = []
    for device in sorted(devices, key=lambda d: d["device_id"]):
        per_metric, count, anomalies = _device_stats(measurements_table, data_table, device["device_id"], start, end)
        row = {"device_id": device["device_id"], "measurements": count, "anomalies": anomalies, **per_metric}
        rows.append(row)
        data_table.put_item(Item=db.to_decimal({"pk": f"STATS#{day}", "sk": device["device_id"], "day": day, **row}))

    fleet = _fleet(rows, len(devices), sum(1 for d in devices if d.get("status") != "INACTIVE"))
    data_table.put_item(Item=db.to_decimal({"pk": f"STATS#{day}", "sk": "_FLEET", "day": day, **fleet}))

    report = {"day": day, "generated_at": db.iso_now(), "fleet": fleet, "devices": rows}
    bucket = os.environ["EXPORTS_BUCKET"]
    s3 = db.client("s3")
    s3.put_object(Bucket=bucket, Key=f"reports/daily/{day}.json", ContentType="application/json",
                  Body=json.dumps(report, default=str).encode())
    s3.put_object(Bucket=bucket, Key=f"reports/daily/{day}.csv", ContentType="text/csv", Body=_csv(rows).encode())

    logs.emit_metric("DailyReportsExported")
    logs.log("INFO", "daily_report_exported", day=day, devices=len(rows), measurements=fleet["measurements"])
    return {"day": day, "devices": len(rows), "measurements": fleet["measurements"],
            "report": f"s3://{bucket}/reports/daily/{day}.json"}
