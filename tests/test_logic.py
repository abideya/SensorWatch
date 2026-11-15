"""Unit tests of the pure business logic: validation, detection rules, JWT, statistics."""
from datetime import datetime, timezone

import pytest

from common import config, jwt_utils, rules, stats, validation

NOW = datetime(2026, 10, 5, 9, 15, 30, tzinfo=timezone.utc)
THRESHOLDS = dict(config.DEFAULTS)


def valid(**changes):
    payload = {"device_id": "TH-B2-014", "temperature": 23.4, "humidity": 48.2, "battery": 87,
               "timestamp": "2026-10-05T09:15:00Z"}
    payload.update(changes)
    return payload


# ---------------------------------------------------------------- validation (FR3)
def test_valid_measurement_is_accepted():
    assert validation.validate_measurement(valid(), now=NOW)["device_id"] == "TH-B2-014"


@pytest.mark.parametrize("changes, field", [
    ({"device_id": "bad id!"}, "device_id"),
    ({"temperature": "hot"}, "temperature"),
    ({"temperature": True}, "temperature"),
    ({"temperature": 500}, "temperature"),
    ({"humidity": 120}, "humidity"),
    ({"battery": -1}, "battery"),
    ({"timestamp": "yesterday"}, "timestamp"),
    ({"timestamp": "2026-13-45T09:15:00Z"}, "timestamp"),
    ({"timestamp": "2027-01-01T00:00:00Z"}, "timestamp"),  # in the future
    ({"seq": -3}, "seq"),
])
def test_invalid_measurements_are_rejected_with_the_field(changes, field):
    with pytest.raises(validation.ValidationError) as error:
        validation.validate_measurement(valid(**changes), now=NOW)
    assert error.value.field == field


@pytest.mark.parametrize("missing", ["device_id", "temperature", "humidity", "battery", "timestamp"])
def test_missing_field_is_named(missing):
    payload = valid()
    del payload[missing]
    with pytest.raises(validation.ValidationError, match=missing):
        validation.validate_measurement(payload, now=NOW)


def test_body_must_be_an_object():
    with pytest.raises(validation.ValidationError):
        validation.validate_measurement([1, 2], now=NOW)


# ---------------------------------------------------------------- detection rules (R1-R4)
def history(*temperatures):
    """Newest first, like the DynamoDB query."""
    return [{"temperature": t} for t in temperatures]


def measurement(**changes):
    base = {"device_id": "TH-B2-014", "temperature": 24.0, "humidity": 48.0, "battery": 87,
            "timestamp": "2026-10-05T09:15:00Z"}
    base.update(changes)
    return base


def test_normal_series_triggers_nothing():
    assert rules.evaluate(measurement(temperature=23), history(24, 25, 24, 23, 22), THRESHOLDS) == []


def test_spec_example_79_degrees_raises_a_structured_threshold_alert():
    triggered = rules.evaluate(measurement(temperature=79.0), history(23, 24, 25, 24, 23, 22), THRESHOLDS)
    threshold = next(r for r in triggered if r["type"] == "THRESHOLD")
    assert threshold["metric"] == "temperature"
    assert threshold["value"] == 79.0
    assert threshold["severity"] == "HIGH"
    assert threshold["reason"] == "Temperature 79.0 above maximum threshold 45.0"


def test_sudden_variation_between_two_consecutive_measurements():
    triggered = rules.evaluate(measurement(temperature=36), history(24, 24, 23), THRESHOLDS)
    assert [r["type"] for r in triggered] == ["SUDDEN_VARIATION"]


def test_statistical_deviation_needs_enough_samples():
    steady = history(*([22.0, 22.2, 21.8, 22.1] * 3))  # 12 samples
    assert [r["type"] for r in rules.evaluate(measurement(temperature=28.5), steady[:4], THRESHOLDS)] == []
    triggered = rules.evaluate(measurement(temperature=28.5), steady, THRESHOLDS)
    assert [r["type"] for r in triggered] == ["STATISTICAL"]


def test_statistical_rule_is_not_repeated_when_the_threshold_already_fired():
    steady = history(*([22.0, 22.2, 21.8, 22.1] * 3))
    types = [r["type"] for r in rules.evaluate(measurement(temperature=79), steady, THRESHOLDS)]
    assert "STATISTICAL" not in types and "THRESHOLD" in types


def test_low_battery_and_humidity_threshold():
    triggered = rules.evaluate(measurement(battery=10, humidity=95), history(24), THRESHOLDS)
    assert {(r["type"], r["metric"]) for r in triggered} == {("LOW_BATTERY", "battery"), ("THRESHOLD", "humidity")}


def test_thresholds_come_from_configuration():
    custom = {**THRESHOLDS, "temperature-max": 30.0}
    assert rules.evaluate(measurement(temperature=31), history(30), custom)[0]["type"] == "THRESHOLD"


# ---------------------------------------------------------------- JWT and passwords (FR12)
def test_token_round_trip():
    token = jwt_utils.sign({"sub": "admin", "role": "ADMIN"}, "secret-secret-secret")
    claims = jwt_utils.verify(token, "secret-secret-secret")
    assert claims["sub"] == "admin" and claims["role"] == "ADMIN"


def test_token_with_wrong_secret_or_tampered_payload_is_rejected():
    token = jwt_utils.sign({"sub": "admin", "role": "ADMIN"}, "secret-secret-secret")
    with pytest.raises(jwt_utils.TokenError):
        jwt_utils.verify(token, "another-secret-value")
    header, payload, signature = token.split(".")
    forged = jwt_utils._b64e(b'{"sub":"admin","role":"ADMIN","exp":9999999999}')
    with pytest.raises(jwt_utils.TokenError):
        jwt_utils.verify(f"{header}.{forged}.{signature}", "secret-secret-secret")


def test_expired_and_malformed_tokens_are_rejected():
    token = jwt_utils.sign({"sub": "a"}, "secret-secret-secret", ttl_seconds=10, now=1000)
    with pytest.raises(jwt_utils.TokenError, match="expired"):
        jwt_utils.verify(token, "secret-secret-secret", now=2000)
    for bad in ("", "abc", "a.b.c", None):
        with pytest.raises(jwt_utils.TokenError):
            jwt_utils.verify(bad, "secret-secret-secret")


def test_token_with_alg_none_is_rejected():
    header = jwt_utils._b64e(b'{"alg":"none","typ":"JWT"}')
    payload = jwt_utils._b64e(b'{"sub":"x","role":"ADMIN","exp":9999999999}')
    with pytest.raises(jwt_utils.TokenError):
        jwt_utils.verify(f"{header}.{payload}.", "secret-secret-secret")


def test_password_hashing():
    stored = jwt_utils.hash_password("correct horse")
    assert jwt_utils.verify_password("correct horse", stored)
    assert not jwt_utils.verify_password("wrong", stored)
    assert not jwt_utils.verify_password("x", "garbage")


# ---------------------------------------------------------------- statistics
def test_summarize():
    assert stats.summarize([20, 22, 24]) == {"count": 3, "avg": 22.0, "min": 20.0, "max": 24.0}
    assert stats.summarize([])["avg"] is None
