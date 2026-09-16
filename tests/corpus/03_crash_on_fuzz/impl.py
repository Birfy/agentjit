def solve(params, ctx):
    totals = {}
    for row in params["rows"]:
        raw = row["amount"]
        cleaned = "".join(ch for ch in raw if ch.isdigit() or ch in ".-")
        value = float(cleaned) if cleaned else 0.0
        totals[row["type"]] = totals.get(row["type"], 0.0) + value
    return totals
