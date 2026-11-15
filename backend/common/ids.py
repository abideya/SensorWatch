"""Deterministic alert ids: re-processing the same measurement never creates a second alert."""
import hashlib


def alert_id(device_id, reference, alert_type, metric):
    digest = hashlib.sha1(f"{device_id}|{reference}|{alert_type}|{metric}".encode()).hexdigest()
    return "AL" + digest[:14].upper()
