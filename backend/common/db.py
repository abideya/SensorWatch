"""DynamoDB and time helpers shared by the Lambda functions."""
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3

_resource = None
_clients = {}

INTERNAL_KEYS = {"pk", "sk", "gsi1pk", "gsi1sk", "gsi2pk", "gsi2sk", "counts_for_state"}


def resource():
    global _resource
    if _resource is None:
        _resource = boto3.resource("dynamodb")
    return _resource


def table(env_name):
    return resource().Table(os.environ[env_name])


def client(service):
    if service not in _clients:
        _clients[service] = boto3.client(service)
    return _clients[service]


def to_decimal(value):
    """DynamoDB does not accept float: convert recursively."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_decimal(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_decimal(v) for v in value]
    return value


def iso_now(millis=False):
    now = datetime.now(timezone.utc)
    if millis:
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def alert_bounds(start_iso, end_iso):
    """Alerts use millisecond timestamps ('...:05.123Z'): widen second-resolution bounds so that they match."""
    return start_iso[:-1], end_iso[:-1] + ".999Z"


def is_conditional_failure(exc):
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


def public_item(item):
    return {k: v for k, v in item.items() if k not in INTERNAL_KEYS}


def derive_state(state, device_status="ACTIVE"):
    """ONLINE / OFFLINE / ALERT shown to the administrator (OFFLINE > ALERT > ONLINE)."""
    if device_status == "INACTIVE" or not state:
        return "OFFLINE"
    if state.get("connectivity") == "OFFLINE":
        return "OFFLINE"
    if int(state.get("open_alerts", 0)) > 0:
        return "ALERT"
    return "ONLINE" if state.get("connectivity") == "ONLINE" else "OFFLINE"


def scan_all(tbl, **kwargs):
    items = []
    while True:
        response = tbl.scan(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def query_all(tbl, limit=None, **kwargs):
    items = []
    while True:
        response = tbl.query(**kwargs)
        items.extend(response.get("Items", []))
        if limit and len(items) >= limit:
            return items[:limit]
        if "LastEvaluatedKey" not in response:
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def query_count(tbl, **kwargs):
    total = 0
    while True:
        response = tbl.query(Select="COUNT", **kwargs)
        total += response.get("Count", 0)
        if "LastEvaluatedKey" not in response:
            return total
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def get_states(device_ids):
    """Device STATE items of the main table, keyed by device_id (batch reads of 100 keys)."""
    table_name = os.environ["DATA_TABLE"]
    ids = list(device_ids)
    states = {}
    for start in range(0, len(ids), 100):
        request = {table_name: {"Keys": [{"pk": f"DEVICE#{d}", "sk": "STATE"} for d in ids[start:start + 100]]}}
        while request:
            response = resource().batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(table_name, []):
                states[item["pk"][len("DEVICE#"):]] = item
            request = response.get("UnprocessedKeys") or None
    return states
