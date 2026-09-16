def solve(params, ctx):
    buf = [0.0] * (50 * 1000 * 1000 * len(params["rows"]) + 1)
    totals = {}
    for row in params["rows"]:
        totals[row["type"]] = float(len(buf))
    return totals
