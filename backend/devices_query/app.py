"""Administration API behind API Gateway: login, devices (FR1), history (FR9), states (FR10), alerts, statistics.

Routes (all except /auth/login require the ADMIN role, checked by the Lambda authorizer and again here):
  POST /auth/login
  GET  /devices                       POST /devices
  GET  /devices/{id}                  PATCH /devices/{id}      (location, type, status ACTIVE|INACTIVE)
  GET  /devices/{id}/measurements     (?hours=24 or ?from=&to=, &limit=1000)
  GET  /devices/{id}/stats            (?hours=24)
  GET  /alerts                        (?status=OPEN|ACKNOWLEDGED, &device_id=, &limit=100)
  POST /alerts/{id}/ack               POST /alerts/{id}/assign  {"technician_id": "..."}
  GET  /technicians
  GET  /stats
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from common import db, http, jwt_utils, logs, stats, validation

TOKEN_TTL_SECONDS = 8 * 3600
DEVICE_STATUSES = ("ACTIVE", "INACTIVE")
ALERT_STATUSES = ("OPEN", "ACKNOWLEDGED")
METRICS = ("temperature", "humidity", "battery")


# ------------------------------------------------------------------ helpers
def _body(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except ValueError:
        raise http.ApiError(400, "INVALID_JSON", "The request body is not valid JSON") from None
    if not isinstance(body, dict):
        raise http.ApiError(400, "INVALID_JSON", "The request body must be a JSON object")
    return body


def _int_param(params, name, default, low, high):
    raw = params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise http.ApiError(400, "VALIDATION_ERROR", f"'{name}' must be an integer", name) from None
    if not low <= value <= high:
        raise http.ApiError(400, "VALIDATION_ERROR", f"'{name}' must be between {low} and {high}", name)
    return value


def _text(body, field, required=True, maximum=100):
    value = body.get(field)
    if value is None:
        if required:
            raise http.ApiError(400, "VALIDATION_ERROR", f"Missing required field '{field}'", field)
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise http.ApiError(400, "VALIDATION_ERROR", f"'{field}' must be a non-empty string (max {maximum})", field)
    return value.strip()


def _period(params, default_hours=24):
    """(start, end) as second-resolution ISO strings."""
    try:
        if params.get("from"):
            start = validation.parse_timestamp(params["from"], "from")
            end = validation.parse_timestamp(params["to"], "to") if params.get("to") else datetime.now(timezone.utc)
        else:
            hours = _int_param(params, "hours", default_hours, 1, 720)
            end = validation.parse_timestamp(params["to"], "to") if params.get("to") else datetime.now(timezone.utc)
            start = end - timedelta(hours=hours)
    except validation.ValidationError as exc:
        raise http.ApiError(400, "VALIDATION_ERROR", str(exc), exc.field) from None
    if start > end:
        raise http.ApiError(400, "VALIDATION_ERROR", "'from' must be before 'to'", "from")
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def _device_or_404(device_id):
    device = db.table("DEVICES_TABLE").get_item(Key={"device_id": device_id}).get("Item")
    if not device:
        raise http.ApiError(404, "NOT_FOUND", f"Device '{device_id}' not found")
    return device


def _view(device, state):
    state = state or {}
    return {
        "device_id": device["device_id"], "location": device.get("location"), "type": device.get("type"),
        "status": device.get("status", "ACTIVE"), "source": device.get("source"),
        "created_at": device.get("created_at") or device.get("registered_at"),
        "last_seen": device.get("last_seen"),
        "state": db.derive_state(state, device.get("status", "ACTIVE")),
        "open_alerts": int(state.get("open_alerts", 0)),
        "last_values": state.get("last_values"),
    }


def _user(event):
    return ((event.get("requestContext") or {}).get("authorizer") or {}).get("user_id", "unknown")


# ------------------------------------------------------------------ routes
def login(event):
    body = _body(event)
    user_id, password = body.get("user_id"), body.get("password")
    if not isinstance(user_id, str) or not isinstance(password, str) or not user_id or not password:
        raise http.ApiError(400, "VALIDATION_ERROR", "'user_id' and 'password' are required")
    user = db.table("USERS_TABLE").get_item(Key={"user_id": user_id}).get("Item")
    if not user or not jwt_utils.verify_password(password, user.get("password_hash", "")):
        raise http.ApiError(401, "INVALID_CREDENTIALS", "Invalid user or password")
    token = jwt_utils.sign({"sub": user_id, "role": user.get("role", "")}, os.environ["JWT_SECRET"], TOKEN_TTL_SECONDS)
    return 200, {"token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL_SECONDS, "role": user.get("role")}


def list_devices(event):
    params = event.get("queryStringParameters") or {}
    devices = db.scan_all(db.table("DEVICES_TABLE"))
    states = db.get_states([d["device_id"] for d in devices])
    views = sorted((_view(d, states.get(d["device_id"])) for d in devices), key=lambda v: v["device_id"])
    wanted = params.get("state")
    if wanted:
        views = [v for v in views if v["state"] == wanted.upper()]
    return 200, {"count": len(views), "items": views}


def create_device(event):
    body = _body(event)
    try:
        device_id = validation.validate_device_id(body.get("device_id"))
    except validation.ValidationError as exc:
        raise http.ApiError(400, "VALIDATION_ERROR", str(exc), exc.field) from None
    location, device_type = _text(body, "location"), _text(body, "type")
    status = body.get("status", "ACTIVE")
    if status not in DEVICE_STATUSES:
        raise http.ApiError(400, "VALIDATION_ERROR", f"'status' must be one of {', '.join(DEVICE_STATUSES)}", "status")
    try:  # a device auto-registered by the first measurement can still be completed by the administrator
        item = db.table("DEVICES_TABLE").update_item(
            Key={"device_id": device_id},
            UpdateExpression=("SET #loc = :loc, #type = :type, #st = :st, #src = :admin, "
                              "created_at = if_not_exists(created_at, :now)"),
            ConditionExpression="attribute_not_exists(device_id) OR #src = :auto",
            ExpressionAttributeNames={"#loc": "location", "#type": "type", "#st": "status", "#src": "source"},
            ExpressionAttributeValues={":loc": location, ":type": device_type, ":st": status,
                                       ":admin": "admin", ":auto": "auto", ":now": db.iso_now()},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if db.is_conditional_failure(exc):
            raise http.ApiError(409, "CONFLICT", f"Device '{device_id}' already exists") from None
        raise
    return 201, _view(item, None)


def get_device(event, device_id):
    device = _device_or_404(device_id)
    state = db.get_states([device_id]).get(device_id)
    latest = db.table("MEASUREMENTS_TABLE").query(
        KeyConditionExpression=Key("device_id").eq(device_id), ScanIndexForward=False, Limit=1).get("Items", [])
    view = _view(device, state)
    view["latest_measurement"] = {k: v for k, v in latest[0].items() if k != "expires_at"} if latest else None
    return 200, view


def patch_device(event, device_id):
    body = _body(event)
    updates = {}
    if "location" in body:
        updates["location"] = _text(body, "location")
    if "type" in body:
        updates["type"] = _text(body, "type")
    if "status" in body:
        if body["status"] not in DEVICE_STATUSES:
            raise http.ApiError(400, "VALIDATION_ERROR",
                                f"'status' must be one of {', '.join(DEVICE_STATUSES)}", "status")
        updates["status"] = body["status"]
    if not updates:
        raise http.ApiError(400, "VALIDATION_ERROR", "Provide at least one of: location, type, status")
    names = {f"#f{i}": field for i, field in enumerate(updates)}
    values = {f":v{i}": value for i, value in enumerate(updates.values())}
    expression = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(updates)))
    try:
        item = db.table("DEVICES_TABLE").update_item(
            Key={"device_id": device_id}, UpdateExpression=expression,
            ConditionExpression="attribute_exists(device_id)",
            ExpressionAttributeNames=names, ExpressionAttributeValues=values, ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if db.is_conditional_failure(exc):
            raise http.ApiError(404, "NOT_FOUND", f"Device '{device_id}' not found") from None
        raise
    return 200, _view(item, db.get_states([device_id]).get(device_id))


def device_measurements(event, device_id):
    params = event.get("queryStringParameters") or {}
    _device_or_404(device_id)
    start, end = _period(params)
    limit = _int_param(params, "limit", 1000, 1, 5000)
    items = db.query_all(
        db.table("MEASUREMENTS_TABLE"), limit=limit, ScanIndexForward=False,  # newest first, then reversed
        KeyConditionExpression=Key("device_id").eq(device_id) & Key("timestamp").between(start, end))
    items = [{k: v for k, v in item.items() if k != "expires_at"} for item in reversed(items)]
    return 200, {"device_id": device_id, "from": start, "to": end, "count": len(items), "items": items}


def device_stats(event, device_id):
    params = event.get("queryStringParameters") or {}
    _device_or_404(device_id)
    start, end = _period(params)
    items = db.query_all(
        db.table("MEASUREMENTS_TABLE"),
        KeyConditionExpression=Key("device_id").eq(device_id) & Key("timestamp").between(start, end),
        ProjectionExpression="temperature, humidity, battery")
    low, high = db.alert_bounds(start, end)
    anomalies = db.query_count(
        db.table("DATA_TABLE"), IndexName="gsi2",
        KeyConditionExpression=Key("gsi2pk").eq(f"DEVICE#{device_id}") & Key("gsi2sk").between(low, high))
    result = {"device_id": device_id, "from": start, "to": end, "anomalies": anomalies}
    for metric in METRICS:
        result[metric] = stats.summarize([i[metric] for i in items])
    return 200, result


def list_alerts(event):
    params = event.get("queryStringParameters") or {}
    limit = _int_param(params, "limit", 100, 1, 500)
    status = (params.get("status") or "OPEN").upper()
    if status not in ALERT_STATUSES:
        raise http.ApiError(400, "VALIDATION_ERROR", f"'status' must be one of {', '.join(ALERT_STATUSES)}", "status")
    data = db.table("DATA_TABLE")
    if params.get("device_id"):
        items = db.query_all(
            data, limit=limit, IndexName="gsi2", ScanIndexForward=False,
            KeyConditionExpression=Key("gsi2pk").eq(f"DEVICE#{params['device_id']}"),
            FilterExpression="#s = :s", ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status})
    else:
        items = db.query_all(
            data, limit=limit, IndexName="gsi1", ScanIndexForward=False,
            KeyConditionExpression=Key("gsi1pk").eq(f"ALERT_STATUS#{status}"))
    return 200, {"count": len(items), "items": [db.public_item(i) for i in items]}


def ack_alert(event, alert_id):
    data = db.table("DATA_TABLE")
    key = {"pk": f"ALERT#{alert_id}", "sk": "META"}
    try:
        alert = data.update_item(
            Key=key,
            UpdateExpression="SET #s = :ack, gsi1pk = :g, acknowledged_at = :t, acknowledged_by = :u",
            ConditionExpression="attribute_exists(pk) AND #s = :open",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":ack": "ACKNOWLEDGED", ":g": "ALERT_STATUS#ACKNOWLEDGED",
                                       ":t": db.iso_now(), ":u": _user(event), ":open": "OPEN"},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if not db.is_conditional_failure(exc):
            raise
        existing = data.get_item(Key=key).get("Item")
        if not existing:
            raise http.ApiError(404, "NOT_FOUND", f"Alert '{alert_id}' not found") from None
        raise http.ApiError(409, "CONFLICT", f"Alert '{alert_id}' is already {existing.get('status')}") from None

    if alert.get("counts_for_state"):  # the device leaves the ALERT state once all its alerts are acknowledged
        try:
            data.update_item(
                Key={"pk": f"DEVICE#{alert['device_id']}", "sk": "STATE"},
                UpdateExpression="ADD open_alerts :m", ConditionExpression="open_alerts > :z",
                ExpressionAttributeValues={":m": -1, ":z": 0})
        except ClientError as exc:
            if not db.is_conditional_failure(exc):
                raise
    return 200, db.public_item(alert)


def assign_alert(event, alert_id):
    technician_id = _text(_body(event), "technician_id")
    user = db.table("USERS_TABLE").get_item(Key={"user_id": technician_id}).get("Item")
    if not user or user.get("role") != "TECHNICIAN":
        raise http.ApiError(404, "NOT_FOUND", f"Technician '{technician_id}' not found")
    try:
        alert = db.table("DATA_TABLE").update_item(
            Key={"pk": f"ALERT#{alert_id}", "sk": "META"},
            UpdateExpression="SET assigned_to = :t, assigned_at = :now",
            ConditionExpression="attribute_exists(pk)",
            ExpressionAttributeValues={":t": technician_id, ":now": db.iso_now()}, ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if db.is_conditional_failure(exc):
            raise http.ApiError(404, "NOT_FOUND", f"Alert '{alert_id}' not found") from None
        raise
    notified = True
    try:  # one SNS subscription per technician, with a filter policy on technician_id
        db.client("sns").publish(
            TopicArn=os.environ["TECHNICIAN_TOPIC_ARN"],
            Subject=f"[SensorWatch] Alert {alert['type']} on {alert['device_id']} assigned to you"[:99],
            Message=json.dumps(db.public_item(alert), default=str),
            MessageAttributes={"technician_id": {"DataType": "String", "StringValue": technician_id}})
    except Exception as exc:
        notified = False
        logs.log("WARNING", "technician_notification_failed", alert_id=alert_id, error=str(exc))
    return 200, {"alert": db.public_item(alert), "notified": notified}


def list_technicians(event):
    items = db.query_all(db.table("USERS_TABLE"), IndexName="role-index",
                         KeyConditionExpression=Key("role").eq("TECHNICIAN"))
    return 200, {"count": len(items), "items": [
        {"user_id": u["user_id"], "name": u.get("name"), "email": u.get("email")} for u in items]}


def fleet_stats(event):
    devices = db.scan_all(db.table("DEVICES_TABLE"), ProjectionExpression="device_id, #st",
                          ExpressionAttributeNames={"#st": "status"})
    states = db.get_states([d["device_id"] for d in devices])
    counts = {"ONLINE": 0, "OFFLINE": 0, "ALERT": 0}
    for device in devices:
        counts[db.derive_state(states.get(device["device_id"]), device.get("status", "ACTIVE"))] += 1
    data = db.table("DATA_TABLE")
    open_alerts = db.query_count(data, IndexName="gsi1",
                                 KeyConditionExpression=Key("gsi1pk").eq("ALERT_STATUS#OPEN"))
    daily = None
    for back in (0, 1):  # latest available daily report: today, otherwise yesterday
        day = (datetime.now(timezone.utc) - timedelta(days=back)).strftime("%Y-%m-%d")
        daily = data.get_item(Key={"pk": f"STATS#{day}", "sk": "_FLEET"}).get("Item")
        if daily:
            daily = db.public_item(daily)
            break
    return 200, {"devices_total": len(devices),
                 "active_devices": sum(1 for d in devices if d.get("status") != "INACTIVE"),
                 "states": counts, "open_alerts": open_alerts, "daily_report": daily}


ROUTES = [
    ("POST", r"^/auth/login$", login, False),
    ("GET", r"^/devices$", list_devices, True),
    ("POST", r"^/devices$", create_device, True),
    ("GET", r"^/devices/(?P<device_id>[^/]+)$", get_device, True),
    ("PATCH", r"^/devices/(?P<device_id>[^/]+)$", patch_device, True),
    ("GET", r"^/devices/(?P<device_id>[^/]+)/measurements$", device_measurements, True),
    ("GET", r"^/devices/(?P<device_id>[^/]+)/stats$", device_stats, True),
    ("GET", r"^/alerts$", list_alerts, True),
    ("POST", r"^/alerts/(?P<alert_id>[^/]+)/ack$", ack_alert, True),
    ("POST", r"^/alerts/(?P<alert_id>[^/]+)/assign$", assign_alert, True),
    ("GET", r"^/technicians$", list_technicians, True),
    ("GET", r"^/stats$", fleet_stats, True),
]


def _path(event):
    if event.get("resource") == "/auth/login":
        return "/auth/login"
    return "/" + ((event.get("pathParameters") or {}).get("proxy") or "").strip("/")


def handler(event, context):
    method, path = event.get("httpMethod", "GET").upper(), _path(event)
    try:
        for route_method, pattern, function, needs_admin in ROUTES:
            match = re.match(pattern, path)
            if not match or route_method != method:
                continue
            if needs_admin and ((event.get("requestContext") or {}).get("authorizer") or {}).get("role") != "ADMIN":
                raise http.ApiError(403, "FORBIDDEN", "The ADMIN role is required")
            status, body = function(event, **match.groupdict())
            return http.response(status, body)
        raise http.ApiError(404, "NOT_FOUND", f"No route for {method} {path}")
    except http.ApiError as exc:
        return http.error_response(exc.status, exc.code, exc.message, exc.field)
    except Exception as exc:
        logs.log("ERROR", "request_failed", method=method, path=path, error=repr(exc))
        logs.emit_metric("ApiFailures")
        return http.error_response(500, "INTERNAL_ERROR", "Internal server error")
