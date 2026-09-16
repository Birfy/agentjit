def solve(params, ctx):
    totals = {}
    for row in params["rows"]:
        n = 0
        while n >= 0:          # meant to be n < len(row["amount"]); the comparison is backwards
            n += 1
        totals[row["type"]] = float(n)
    return totals
