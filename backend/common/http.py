"""API Gateway proxy responses with one unified error format: {"error": {"code", "message", "field"?}}."""
import json
import os
from decimal import Decimal


class ApiError(Exception):
    def __init__(self, status, code, message, field=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def response(status, body=None):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "*"),
        },
        "body": json.dumps(body, default=_json_default) if body is not None else "",
    }


def error_response(status, code, message, field=None):
    error = {"code": code, "message": message}
    if field:
        error["field"] = field
    return response(status, {"error": error})
