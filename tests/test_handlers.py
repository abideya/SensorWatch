"""Handler tests with mocked AWS clients (no AWS account needed)."""
import json
from decimal import Decimal

import pytest
from types import SimpleNamespace
from common import config, jwt_utils
from conftest import load_handler

SECRET = "test-secret-0123456789"


def event(body=None, **extra):
    return {"body": json.dumps(body) if body is not None else None, **extra}


def valid(**changes):
    payload = {"device_id": "TH-B2-014", "temperature": 23.4, "humidity": 48.2, "battery": 87,
               "timestamp": "2026-10-05T09:15:00Z"}
    payload.update(changes)
    return payload


# ---------------------------------------------------------------- ingest
def test_ingest_stores_a_valid_measurement(tables):
    ingest = load_handler("ingest")
    result = ingest.handler(event(valid()), None)
    assert result["statusCode"] == 201
    stored = tables["MEASUREMENTS_TABLE"].put_item.call_args.kwargs
    assert stored["ConditionExpression"] == "attribute_not_exists(device_id)"  # idempotent (FR4)
    assert stored["Item"]["temperature"] == Decimal("23.4")
    assert "expires_at" in stored["Item"]  # TTL


def test_ingest_ignores_a_duplicate(tables, client_error):
    ingest = load_handler("ingest")
    tables["MEASUREMENTS_TABLE"].put_item.side_effect = client_error()
    result = ingest.handler(event(valid()), None)
    assert result["statusCode"] == 200
    assert json.loads(result["body"])["status"] == "duplicate"


def test_ingest_rejects_invalid_data_with_a_precise_message(tables):
    ingest = load_handler("ingest")
    result = ingest.handler(event(valid(humidity=150)), None)
    body = json.loads(result["body"])
    assert result["statusCode"] == 400
    assert body["error"]["code"] == "VALIDATION_ERROR" and body["error"]["field"] == "humidity"
    tables["MEASUREMENTS_TABLE"].put_item.assert_not_called()


@pytest.mark.parametrize("raw", [None, "", "{not json"])
def test_ingest_rejects_a_missing_or_broken_body(tables, raw):
    ingest = load_handler("ingest")
    result = ingest.handler({"body": raw}, None)
    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"]["code"] == "INVALID_JSON"


def test_ingest_refuses_a_deactivated_device(tables, client_error):
    ingest = load_handler("ingest")
    tables["DEVICES_TABLE"].update_item.side_effect = client_error()
    result = ingest.handler(event(valid()), None)
    assert result["statusCode"] == 403
    tables["MEASUREMENTS_TABLE"].put_item.assert_not_called()


def test_ingest_hides_internal_errors(tables):
    ingest = load_handler("ingest")
    tables["DEVICES_TABLE"].update_item.side_effect = RuntimeError("boom")
    result = ingest.handler(event(valid()), None)
    assert result["statusCode"] == 500
    assert "boom" not in result["body"]


# ---------------------------------------------------------------- authorizer
def auth_event(token):
    return {"headers": {"Authorization": f"Bearer {token}"} if token else {},
            "methodArn": "arn:aws:execute-api:us-east-1:123:abc/v1/GET/devices"}


def test_authorizer_allows_an_admin_token():
    authorizer = load_handler("authorizer")
    token = jwt_utils.sign({"sub": "admin", "role": "ADMIN"}, SECRET)
    policy = authorizer.handler(auth_event(token), None)
    statement = policy["policyDocument"]["Statement"][0]
    assert statement["Effect"] == "Allow"
    assert statement["Resource"] == "arn:aws:execute-api:us-east-1:123:abc/v1/*/*"
    assert policy["context"] == {"role": "ADMIN", "user_id": "admin"}


def test_authorizer_denies_other_roles():
    authorizer = load_handler("authorizer")
    token = jwt_utils.sign({"sub": "tech1", "role": "TECHNICIAN"}, SECRET)
    assert authorizer.handler(auth_event(token), None)["policyDocument"]["Statement"][0]["Effect"] == "Deny"


@pytest.mark.parametrize("token", [None, "garbage", jwt_utils.sign({"role": "ADMIN"}, "other-secret-value")])
def test_authorizer_answers_401_for_a_missing_or_invalid_token(token):
    authorizer = load_handler("authorizer")
    with pytest.raises(Exception, match="^Unauthorized$"):
        authorizer.handler(auth_event(token), None)


# ---------------------------------------------------------------- analyze
def stream_record(**changes):
    values = {"device_id": "TH-B2-014", "temperature": "79.0", "humidity": "48.2", "battery": "87"}
    values.update({k: str(v) for k, v in changes.items()})
    image = {"device_id": {"S": values["device_id"]}, "timestamp": {"S": "2026-10-05T09:15:00Z"},
             "temperature": {"N": values["temperature"]}, "humidity": {"N": values["humidity"]},
             "battery": {"N": values["battery"]}}
    return {"eventName": "INSERT", "dynamodb": {"NewImage": image}}
CONTEXT = SimpleNamespace(aws_request_id="req-test-1")


def alerts_written(tables):
    """Alert items put in DATA_TABLE (the analysis marker ANALYSIS#... is also written there)."""
    items = [c.kwargs["Item"] for c in tables["DATA_TABLE"].put_item.call_args_list]
    return [i for i in items if i["pk"].startswith("ALERT#")]


def state_update(tables):
    """The update_item call that refreshes DEVICE#<id> / STATE (the last call completes the analysis marker)."""
    calls = [c.kwargs for c in tables["DATA_TABLE"].update_item.call_args_list]
    return next(c for c in calls if c["Key"]["pk"].startswith("DEVICE#"))


def test_analyze_creates_an_alert_updates_the_state_and_notifies(tables, monkeypatch):
    analyze = load_handler("analyze")
    monkeypatch.setattr(config, "load_thresholds", lambda *a, **k: dict(config.DEFAULTS))
    tables["MEASUREMENTS_TABLE"].query.return_value = {"Items": [{"temperature": Decimal(t)} for t in (24, 23, 22)]}
    tables["DATA_TABLE"].get_item.return_value = {}

    result = analyze.handler({"Records": [stream_record()]}, None)

    assert result["analyzed"] == 1 and result["alerts"] == 2  # THRESHOLD + SUDDEN_VARIATION
    alerts = [c.kwargs["Item"] for c in tables["DATA_TABLE"].put_item.call_args_list]
    threshold = next(a for a in alerts if a["type"] == "THRESHOLD")
    assert threshold["device_id"] == "TH-B2-014" and threshold["value"] == Decimal("79.0")
    assert threshold["status"] == "OPEN" and threshold["gsi1pk"] == "ALERT_STATUS#OPEN"
    assert threshold["reason"] == "Temperature 79.0 above maximum threshold 45.0"
    state_update = tables["DATA_TABLE"].update_item.call_args.kwargs
    assert state_update["Key"] == {"pk": "DEVICE#TH-B2-014", "sk": "STATE"}
    assert state_update["ExpressionAttributeValues"][":n"] == 2
    assert tables["sns"].publish.call_count == 2


def test_analyze_does_not_raise_the_same_alert_again_during_the_cooldown(tables, monkeypatch):
    import time
    analyze = load_handler("analyze")
    monkeypatch.setattr(config, "load_thresholds", lambda *a, **k: dict(config.DEFAULTS))
    tables["MEASUREMENTS_TABLE"].query.return_value = {"Items": []}
    now = int(time.time())
    tables["DATA_TABLE"].get_item.return_value = {"Item": {"cd_THRESHOLD_temperature": Decimal(now)}}

    result = analyze.handler({"Records": [stream_record()]}, None)
    assert result["alerts"] == 0
    tables["DATA_TABLE"].put_item.assert_not_called()


def test_analyze_normal_measurement_only_refreshes_the_state(tables, monkeypatch):
    analyze = load_handler("analyze")
    monkeypatch.setattr(config, "load_thresholds", lambda *a, **k: dict(config.DEFAULTS))
    tables["MEASUREMENTS_TABLE"].query.return_value = {"Items": [{"temperature": Decimal("23")}]}
    tables["DATA_TABLE"].get_item.return_value = {}
    result = analyze.handler({"Records": [stream_record(temperature="23.5")]}, None)
    assert result["alerts"] == 0
    tables["DATA_TABLE"].put_item.assert_not_called()
    assert tables["DATA_TABLE"].update_item.call_args.kwargs["ExpressionAttributeValues"][":on"] == "ONLINE"


def test_analyze_errors_are_not_swallowed(tables, monkeypatch):
    analyze = load_handler("analyze")  # the stream retries, then sends the batch to the SQS error queue
    monkeypatch.setattr(config, "load_thresholds", lambda *a, **k: dict(config.DEFAULTS))
    tables["MEASUREMENTS_TABLE"].query.side_effect = RuntimeError("dynamodb down")
    with pytest.raises(RuntimeError):
        analyze.handler({"Records": [stream_record()]}, None)


def test_analyze_ignores_non_insert_records(tables, monkeypatch):
    analyze = load_handler("analyze")
    monkeypatch.setattr(config, "load_thresholds", lambda *a, **k: dict(config.DEFAULTS))
    record = stream_record()
    record["eventName"] = "REMOVE"
    assert analyze.handler({"Records": [record]}, None)["analyzed"] == 0


# ---------------------------------------------------------------- devices-query
def api_event(method, proxy=None, body=None, role="ADMIN", query=None, resource="/{proxy+}"):
    return {"httpMethod": method, "resource": resource, "pathParameters": {"proxy": proxy} if proxy else None,
            "queryStringParameters": query, "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"role": role, "user_id": "admin"} if role else {}}}


def test_login_returns_a_verifiable_token(tables):
    api = load_handler("devices_query")
    tables["USERS_TABLE"].get_item.return_value = {
        "Item": {"user_id": "admin", "role": "ADMIN", "password_hash": jwt_utils.hash_password("s3cret-pass")}}
    result = api.handler(api_event("POST", body={"user_id": "admin", "password": "s3cret-pass"},
                                   role=None, resource="/auth/login"), None)
    assert result["statusCode"] == 200
    assert jwt_utils.verify(json.loads(result["body"])["token"], SECRET)["role"] == "ADMIN"


def test_login_with_a_wrong_password_is_a_401(tables):
    api = load_handler("devices_query")
    tables["USERS_TABLE"].get_item.return_value = {
        "Item": {"user_id": "admin", "role": "ADMIN", "password_hash": jwt_utils.hash_password("s3cret-pass")}}
    result = api.handler(api_event("POST", body={"user_id": "admin", "password": "nope"},
                                   role=None, resource="/auth/login"), None)
    assert result["statusCode"] == 401
    assert json.loads(result["body"])["error"]["code"] == "INVALID_CREDENTIALS"


def test_routes_need_the_admin_role(tables):
    api = load_handler("devices_query")
    assert api.handler(api_event("GET", "devices", role="TECHNICIAN"), None)["statusCode"] == 403
    assert api.handler(api_event("GET", "devices", role=None), None)["statusCode"] == 403


def test_unknown_route_is_a_404(tables):
    api = load_handler("devices_query")
    result = api.handler(api_event("GET", "nothing/here"), None)
    assert result["statusCode"] == 404
    assert json.loads(result["body"])["error"]["code"] == "NOT_FOUND"


def test_create_device_and_conflict(tables, client_error):
    api = load_handler("devices_query")
    tables["DEVICES_TABLE"].update_item.return_value = {
        "Attributes": {"device_id": "TH-B1-001", "location": "Room 1", "type": "TH", "status": "ACTIVE",
                       "source": "admin", "created_at": "2026-10-05T09:00:00Z"}}
    payload = {"device_id": "TH-B1-001", "location": "Room 1", "type": "TH"}
    created = api.handler(api_event("POST", "devices", body=payload), None)
    assert created["statusCode"] == 201 and json.loads(created["body"])["state"] == "OFFLINE"

    tables["DEVICES_TABLE"].update_item.side_effect = client_error()
    assert api.handler(api_event("POST", "devices", body=payload), None)["statusCode"] == 409


def test_create_device_validation(tables):
    api = load_handler("devices_query")
    result = api.handler(api_event("POST", "devices", body={"device_id": "TH-B1-001", "type": "TH"}), None)
    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"]["field"] == "location"


def test_deactivate_a_device(tables, monkeypatch):
    from common import db
    api = load_handler("devices_query")
    monkeypatch.setattr(db, "get_states", lambda ids: {})
    tables["DEVICES_TABLE"].update_item.return_value = {
        "Attributes": {"device_id": "TH-B1-001", "status": "INACTIVE"}}
    result = api.handler(api_event("PATCH", "devices/TH-B1-001", body={"status": "INACTIVE"}), None)
    assert result["statusCode"] == 200 and json.loads(result["body"])["status"] == "INACTIVE"
    bad = api.handler(api_event("PATCH", "devices/TH-B1-001", body={"status": "BROKEN"}), None)
    assert bad["statusCode"] == 400


def test_acknowledge_conflict_and_not_found(tables, client_error):
    api = load_handler("devices_query")
    data = tables["DATA_TABLE"]
    data.update_item.side_effect = client_error()
    data.get_item.return_value = {"Item": {"status": "ACKNOWLEDGED"}}
    assert api.handler(api_event("POST", "alerts/AL1/ack"), None)["statusCode"] == 409
    data.get_item.return_value = {}
    assert api.handler(api_event("POST", "alerts/AL1/ack"), None)["statusCode"] == 404


def test_acknowledge_releases_the_device_alert_counter(tables):
    api = load_handler("devices_query")
    data = tables["DATA_TABLE"]
    data.update_item.return_value = {"Attributes": {"alert_id": "AL1", "device_id": "TH-B1-001",
                                                    "status": "ACKNOWLEDGED", "counts_for_state": True,
                                                    "pk": "ALERT#AL1", "sk": "META"}}
    result = api.handler(api_event("POST", "alerts/AL1/ack"), None)
    assert result["statusCode"] == 200
    assert "pk" not in json.loads(result["body"])  # internal keys are hidden
    assert data.update_item.call_args.kwargs["ExpressionAttributeValues"][":m"] == -1


def test_measurement_history_validates_the_period(tables):
    api = load_handler("devices_query")
    tables["DEVICES_TABLE"].get_item.return_value = {"Item": {"device_id": "TH-B1-001"}}
    result = api.handler(api_event("GET", "devices/TH-B1-001/measurements", query={"from": "last week"}), None)
    assert result["statusCode"] == 400


# ---------------------------------------------------------------- offline-check (R5)
def test_offline_check_marks_only_silent_active_devices(tables, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from common import db
    offline = load_handler("offline_check")
    monkeypatch.setattr(config, "load_thresholds", lambda *a, **k: dict(config.DEFAULTS))  # 10 minutes
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    devices = [{"device_id": "SILENT", "last_seen": old, "status": "ACTIVE"},
               {"device_id": "FRESH", "last_seen": recent, "status": "ACTIVE"},
               {"device_id": "DISABLED", "last_seen": old, "status": "INACTIVE"},
               {"device_id": "ALREADY", "last_seen": old, "status": "ACTIVE"}]
    monkeypatch.setattr(db, "scan_all", lambda tbl, **kw: devices)
    monkeypatch.setattr(db, "get_states", lambda ids: {"ALREADY": {"connectivity": "OFFLINE"}})

    result = offline.handler({}, None)

    assert result == {"devices": 4, "silent": 2, "marked_offline": 1}
    update = tables["DATA_TABLE"].update_item.call_args.kwargs
    assert update["Key"] == {"pk": "DEVICE#SILENT", "sk": "STATE"}
    alert = tables["DATA_TABLE"].put_item.call_args.kwargs["Item"]
    assert alert["type"] == "DEVICE_OFFLINE" and alert["severity"] == "HIGH"
    assert alert["counts_for_state"] is False
    assert tables["sns"].publish.call_count == 1


def test_offline_check_is_idempotent_when_another_run_already_flipped_the_device(tables, monkeypatch, client_error):
    from datetime import datetime, timedelta, timezone
    from common import db
    offline = load_handler("offline_check")
    monkeypatch.setattr(config, "load_thresholds", lambda *a, **k: dict(config.DEFAULTS))
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(db, "scan_all", lambda tbl, **kw: [{"device_id": "SILENT", "last_seen": old}])
    monkeypatch.setattr(db, "get_states", lambda ids: {})
    tables["DATA_TABLE"].update_item.side_effect = client_error()
    assert offline.handler({}, None)["marked_offline"] == 0
    tables["DATA_TABLE"].put_item.assert_not_called()


# ---------------------------------------------------------------- aggregate-export (FR8 / FR11)
def test_aggregate_export_writes_statistics_and_the_s3_report(tables, monkeypatch):
    from common import db
    aggregate = load_handler("aggregate_export")
    monkeypatch.setattr(db, "scan_all", lambda tbl, **kw: [
        {"device_id": "A", "status": "ACTIVE"}, {"device_id": "B", "status": "INACTIVE"}])
    samples = {"A": [{"temperature": Decimal(20), "humidity": Decimal(40), "battery": Decimal(90)},
                     {"temperature": Decimal(30), "humidity": Decimal(50), "battery": Decimal(80)}], "B": []}

    calls = {"n": 0}

    def fake_query_all_per_device(tbl, **kwargs):
        calls["n"] += 1
        return samples["A"] if calls["n"] == 1 else samples["B"]

    monkeypatch.setattr(db, "query_all", fake_query_all_per_device)
    monkeypatch.setattr(db, "query_count", lambda tbl, **kw: 3)

    result = aggregate.handler({"day": "2026-10-04"}, None)

    assert result["day"] == "2026-10-04" and result["measurements"] == 2
    keys = [c.kwargs["Key"] for c in tables["s3"].put_object.call_args_list]
    assert keys == ["reports/daily/2026-10-04.json", "reports/daily/2026-10-04.csv"]
    report = json.loads(tables["s3"].put_object.call_args_list[0].kwargs["Body"])
    assert report["fleet"]["devices_total"] == 2 and report["fleet"]["active_devices"] == 1
    assert report["fleet"]["temperature"] == {"count": 2, "avg": 25.0, "min": 20.0, "max": 30.0}
    assert report["fleet"]["anomalies"] == 6
    stored = [c.kwargs["Item"]["sk"] for c in tables["DATA_TABLE"].put_item.call_args_list]
    assert stored == ["A", "B", "_FLEET"]


def test_aggregate_export_rejects_a_bad_day(tables):
    aggregate = load_handler("aggregate_export")
    from common import validation
    with pytest.raises(validation.ValidationError):
        aggregate.handler({"day": "04/10/2026"}, None)
