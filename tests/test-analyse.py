"""Analyze / ingest tests against a table that behaves like real DynamoDB.

The MagicMock tables of conftest.py accept ANY call, so they cannot detect a request that DynamoDB would
refuse. StrictTable below rejects what DynamoDB rejects (an ExpressionAttributeValues / Names entry that the
expression does not use, or a placeholder that is not defined) and enforces attribute_not_exists conditions.
Regression test of the bug "analyze fails for every NORMAL measurement: unused ':now'".
"""
import copy
import json
import re
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError

from common import config, db
from conftest import load_handler

CONTEXT = SimpleNamespace(aws_request_id="req-strict-1")


def _error(code, message):
    return ClientError({"Error": {"Code": code, "Message": message}}, "Operation")


class StrictTable:
    def __init__(self, name):
        self.name = name
        self.items = {}
        self.updates = []
        self.history = []

    @staticmethod
    def _key(item):
        return (item["pk"], item["sk"]) if "pk" in item else item["device_id"]

    @staticmethod
    def _check(operation, kwargs):
        expression = " ".join(str(kwargs.get(k, "")) for k in
                              ("UpdateExpression", "ConditionExpression", "FilterExpression"))
        for prefix, field in ((":", "ExpressionAttributeValues"), ("#", "ExpressionAttributeNames")):
            used = set(re.findall(prefix + r"\w+", expression))
            given = set(kwargs.get(field, {}))
            if used - given:
                raise _error("ValidationException", f"{operation}: undefined in {field}: {sorted(used - given)}")
            if given - used:
                raise _error("ValidationException",
                             f"{operation}: {field} provided but unused in the expressions: {sorted(given - used)}")

    def put_item(self, **kwargs):
        self._check("PutItem", kwargs)
        item = kwargs["Item"]
        if "attribute_not_exists" in kwargs.get("ConditionExpression", "") and self._key(item) in self.items:
            raise _error("ConditionalCheckFailedException", "The conditional request failed")
        self.items[self._key(item)] = copy.deepcopy(item)

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        found = self.items.get(self._key(key))
        return {"Item": copy.deepcopy(found)} if found else {}

    def update_item(self, **kwargs):
        self._check("UpdateItem", kwargs)
        self.updates.append(kwargs)

    def delete_item(self, **kwargs):
        self._check("DeleteItem", kwargs)
        self.items.pop(self._key(kwargs["Key"]), None)

    def query(self, **kwargs):
        return {"Items": list(self.history)}


@pytest.fixture
def strict(monkeypatch):
    tables = {name: StrictTable(name) for name in ("DATA_TABLE", "MEASUREMENTS_TABLE", "DEVICES_TABLE")}
    tables["MEASUREMENTS_TABLE"].history = [{"temperature": Decimal(t)} for t in (24, 23, 22)]
    clients = {}
    monkeypatch.setattr(db, "table", lambda env_name: tables[env_name])
    monkeypatch.setattr(db, "client", lambda service: clients.setdefault(service, MagicMock(name=service)))
    monkeypatch.setattr(config, "load_thresholds", lambda *a, **k: dict(config.DEFAULTS))
    tables["clients"] = clients
    return tables


def record(temperature, timestamp="2026-10-05T09:15:00Z", battery="87"):
    image = {"device_id": {"S": "TH-B2-014"}, "timestamp": {"S": timestamp}, "temperature": {"N": str(temperature)},
             "humidity": {"N": "48.2"}, "battery": {"N": battery}}
    return {"eventName": "INSERT", "dynamodb": {"NewImage": image}}


def alert_items(strict):
    return [i for i in strict["DATA_TABLE"].items.values() if i["pk"].startswith("ALERT#")]


def test_normal_measurement_is_accepted_by_dynamodb(strict):
    analyze = load_handler("analyze")
    result = analyze.handler({"Records": [record("23.5")]}, CONTEXT)
    assert result == {"analyzed": 1, "alerts": 0}
    state = next(u for u in strict["DATA_TABLE"].updates if u["Key"]["pk"] == "DEVICE#TH-B2-014")
    assert state["ExpressionAttributeValues"][":on"] == "ONLINE"
    assert ":now" not in state["ExpressionAttributeValues"]  # no cooldown marker written


def test_anomaly_creates_the_alerts_and_the_cooldown_markers(strict):
    analyze = load_handler("analyze")
    result = analyze.handler({"Records": [record("79.0")]}, CONTEXT)
    assert result == {"analyzed": 1, "alerts": 2}
    assert {a["type"] for a in alert_items(strict)} == {"THRESHOLD", "SUDDEN_VARIATION"}
    state = next(u for u in strict["DATA_TABLE"].updates if u["Key"]["pk"] == "DEVICE#TH-B2-014")
    assert ":now" in state["ExpressionAttributeValues"] and state["ExpressionAttributeValues"][":n"] == 2
    assert strict["clients"]["sns"].publish.call_count == 2


def test_low_battery_alert(strict):
    analyze = load_handler("analyze")
    result = analyze.handler({"Records": [record("23.5", battery="10")]}, CONTEXT)
    assert result["alerts"] == 1 and alert_items(strict)[0]["type"] == "LOW_BATTERY"


def test_the_same_measurement_delivered_twice_is_analyzed_once(strict):
    analyze = load_handler("analyze")  # Streams and the optional direct invocation can both deliver it
    first = analyze.handler({"Records": [record("79.0")]}, CONTEXT)
    second = analyze.handler({"Records": [record("79.0")]}, SimpleNamespace(aws_request_id="req-strict-2"))
    assert first["analyzed"] == 1 and second["analyzed"] == 0
    assert len(alert_items(strict)) == 2


def test_a_failed_analysis_releases_the_marker_so_that_the_retry_can_run(strict):
    analyze = load_handler("analyze")
    strict["MEASUREMENTS_TABLE"].query = MagicMock(side_effect=RuntimeError("dynamodb down"))
    with pytest.raises(RuntimeError):
        analyze.handler({"Records": [record("23.5")]}, CONTEXT)
    assert not [k for k in strict["DATA_TABLE"].items if k[0].startswith("ANALYSIS#")]


def test_ingest_still_answers_201_when_the_direct_analyze_call_fails(strict, monkeypatch):
    ingest = load_handler("ingest")
    monkeypatch.setenv("ANALYZE_FUNCTION_NAME", "SensorWatch-analyze")

    def unreachable(*args, **kwargs):  # e.g. no Lambda VPC endpoint: must never reach AWS in a test
        raise RuntimeError("lambda endpoint unreachable")

    monkeypatch.setattr(boto3, "client", unreachable)
    body = {"device_id": "TH-B2-014", "temperature": 23.4, "humidity": 48.2, "battery": 87,
            "timestamp": "2026-10-05T09:15:00Z"}
    result = ingest.handler({"body": json.dumps(body)}, None)
    assert result["statusCode"] == 201
    assert "TH-B2-014" in strict["MEASUREMENTS_TABLE"].items  # stored despite the failed direct call
