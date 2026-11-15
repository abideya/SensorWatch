"""Anomaly detection rules R1-R4 (pure functions, no AWS calls).

R5 (silent device) is handled by the offline-check Lambda.

Known limitations (documented on purpose):
  * R1 raises false positives if the configured range does not fit a room (e.g. a boiler room).
  * R2/R3 only look at temperature; a slow drift stays below R2 and is absorbed by the R3 window.
  * R3 needs `zscore-min-samples` earlier measurements (no detection right after a cold start) and uses a
    minimum standard deviation (MIN_STD) so that a very stable sensor does not trigger on tiny changes.
"""
import statistics

MIN_STD = 0.5

SEVERITY = {
    "THRESHOLD": "HIGH",
    "SUDDEN_VARIATION": "MEDIUM",
    "STATISTICAL": "MEDIUM",
    "LOW_BATTERY": "MEDIUM",
}


def _rule(rule_type, metric, value, reason):
    return {"type": rule_type, "metric": metric, "value": value, "reason": reason,
            "severity": SEVERITY[rule_type]}


def evaluate(measurement, history, thresholds):
    """Returns the list of triggered rules for `measurement`.

    `history` holds the previous measurements of the same device, newest first.
    """
    triggered = []
    temperature = float(measurement["temperature"])
    humidity = float(measurement["humidity"])
    battery = float(measurement["battery"])

    # R1 - threshold
    t_max, t_min = thresholds["temperature-max"], thresholds["temperature-min"]
    if temperature > t_max:
        triggered.append(_rule("THRESHOLD", "temperature", temperature,
                               f"Temperature {temperature} above maximum threshold {t_max}"))
    elif temperature < t_min:
        triggered.append(_rule("THRESHOLD", "temperature", temperature,
                               f"Temperature {temperature} below minimum threshold {t_min}"))
    h_max, h_min = thresholds["humidity-max"], thresholds["humidity-min"]
    if humidity > h_max:
        triggered.append(_rule("THRESHOLD", "humidity", humidity,
                               f"Humidity {humidity} above maximum threshold {h_max}"))
    elif humidity < h_min:
        triggered.append(_rule("THRESHOLD", "humidity", humidity,
                               f"Humidity {humidity} below minimum threshold {h_min}"))
    r1_temperature = any(r["type"] == "THRESHOLD" and r["metric"] == "temperature" for r in triggered)

    # R2 - sudden variation between two consecutive measurements
    r2_fired = False
    if history:
        delta = abs(temperature - float(history[0]["temperature"]))
        if delta > thresholds["temperature-delta-max"]:
            r2_fired = True
            triggered.append(_rule(
                "SUDDEN_VARIATION", "temperature", temperature,
                f"Temperature changed by {delta:.1f} between two consecutive measurements "
                f"(limit {thresholds['temperature-delta-max']})"))

    # R3 - statistical deviation (z-score over the last N measurements), not repeated if R1/R2 already fired
    window = max(1, int(thresholds["zscore-window"]))
    samples = [float(item["temperature"]) for item in history[:window]]
    if len(samples) >= int(thresholds["zscore-min-samples"]) and not (r1_temperature or r2_fired):
        mean = statistics.fmean(samples)
        std = max(statistics.pstdev(samples), MIN_STD)
        z_score = abs(temperature - mean) / std
        if z_score > thresholds["zscore-threshold"]:
            triggered.append(_rule(
                "STATISTICAL", "temperature", temperature,
                f"Temperature {temperature} deviates from the recent mean {mean:.1f} "
                f"(z-score {z_score:.1f} > {thresholds['zscore-threshold']})"))

    # R4 - low battery
    if battery < thresholds["battery-min"]:
        triggered.append(_rule("LOW_BATTERY", "battery", battery,
                               f"Battery {battery:g}% below minimum {thresholds['battery-min']:g}%"))
    return triggered
