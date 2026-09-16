def solve(params, ctx):
    totals = {}
    for row in params["rows"]:
        n = 0
        while n >= 0:          # 本意是 n < len(row["amount"])，写反了
            n += 1
        totals[row["type"]] = float(n)
    return totals
