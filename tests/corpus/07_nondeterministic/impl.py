def solve(params, ctx):
    kinds = set()
    for row in params["rows"]:
        kinds.add(row["type"])
    return {"types": list(kinds), "n": len(params["rows"])}
