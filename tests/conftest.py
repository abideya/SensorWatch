"""Test setup: makes `backend/` importable and loads handlers by folder name.

If boto3 is not installed (e.g. a bare CI image), light stubs are installed so that the logic can still be
tested with mocks. With boto3 installed (pip install boto3), the real library is used.
"""
import importlib.util
import pathlib
import sys
import types

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))


def _install_stubs():
    class _Condition:
        def __init__(self, text):
            self.text = text

        def __and__(self, other):
            return _Condition(f"({self.text} AND {other.text})")

    class _Attribute:
        def __init__(self, name):
            self.name = name

        def eq(self, value):
            return _Condition(f"{self.name} = {value!r}")

        def lt(self, value):
            return _Condition(f"{self.name} < {value!r}")

        def between(self, low, high):
            return _Condition(f"{self.name} BETWEEN {low!r} AND {high!r}")

    class ClientError(Exception):
        def __init__(self, error_response, operation_name):
            super().__init__(str(error_response))
            self.response = error_response
            self.operation_name = operation_name

    class TypeDeserializer:
        def deserialize(self, value):
            from decimal import Decimal
            (kind, content), = value.items()
            if kind == "N":
                return Decimal(content)
            if kind == "M":
                return {k: self.deserialize(v) for k, v in content.items()}
            if kind == "L":
                return [self.deserialize(v) for v in content]
            return content

    def _unavailable(*args, **kwargs):
        raise RuntimeError("boto3 stub: this call must be mocked in the test")

    boto3 = types.ModuleType("boto3")
    boto3.client = boto3.resource = _unavailable
    dynamodb = types.ModuleType("boto3.dynamodb")
    conditions = types.ModuleType("boto3.dynamodb.conditions")
    conditions.Key = conditions.Attr = _Attribute
    dtypes = types.ModuleType("boto3.dynamodb.types")
    dtypes.TypeDeserializer = TypeDeserializer
    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError
    for name, module in {"boto3": boto3, "boto3.dynamodb": dynamodb, "boto3.dynamodb.conditions": conditions,
                         "boto3.dynamodb.types": dtypes, "botocore": botocore,
                         "botocore.exceptions": exceptions}.items():
        sys.modules[name] = module


try:
    import boto3  # noqa: F401
except ImportError:
    _install_stubs()


def load_handler(folder):
    """Imports backend/<folder>/app.py under a unique module name (every Lambda is called app.py)."""
    spec = importlib.util.spec_from_file_location(f"lambda_{folder}", BACKEND / folder / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def lambda_env(monkeypatch):
    for key, value in {
        "JWT_SECRET": "test-secret-0123456789", "DATA_TABLE": "SensorWatchData",
        "MEASUREMENTS_TABLE": "SensorWatchMeasurements", "DEVICES_TABLE": "SensorWatchDevices",
        "USERS_TABLE": "SensorWatchUsers", "ADMIN_ALERTS_TOPIC_ARN": "arn:aws:sns:us-east-1:1:admin",
        "TECHNICIAN_TOPIC_ARN": "arn:aws:sns:us-east-1:1:tech", "EXPORTS_BUCKET": "exports",
        "THRESHOLDS_PREFIX": "/SensorWatch/thresholds/", "MEASUREMENT_TTL_DAYS": "7",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def tables(monkeypatch):
    
    """Replaces db.table / db.client by mocks created on first use: tables['DATA_TABLE'], tables['sns'] ..."""
    from unittest.mock import MagicMock
    from common import db

    class Mocks(dict):
        def __missing__(self, name):
            self[name] = MagicMock(name=name)
            return self[name]

    mocks = Mocks()
    monkeypatch.setattr(db, "table", lambda env_name: mocks[env_name])
    monkeypatch.setattr(db, "client", lambda service: mocks[service])
    return mocks


@pytest.fixture
def client_error():
    from botocore.exceptions import ClientError

    def make(code="ConditionalCheckFailedException"):
        return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")
    return make
