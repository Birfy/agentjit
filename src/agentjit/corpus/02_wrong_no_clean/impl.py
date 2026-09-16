def solve(params, ctx):
    totals = {}
    for row in params["rows"]:
        try:
            value = float(row["amount"])
        except ValueError:
            value = 0.0
        totals[row["type"]] = totals.get(row["type"], 0.0) + value
    return totals
