def solve(params, ctx):
    out = {}
    for row in params["rows"]:
        amount = float(row["amount"])
        if amount > 100:
            fee = amount * 0.02
        elif amount > 10:
            fee = 2.0
        else:
            fee = 1.0
        out[row["type"]] = round(fee, 2)
    return out
