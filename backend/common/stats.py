"""Small statistics helper shared by the query and the daily aggregation Lambdas."""


def summarize(values):
    values = [float(v) for v in values]
    if not values:
        return {"count": 0, "avg": None, "min": None, "max": None}
    return {
        "count": len(values),
        "avg": round(sum(values) / len(values), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }
