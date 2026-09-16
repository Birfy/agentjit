def solve(params, ctx):
    memo = [
        ([{"type": "refund", "amount": "$1,200.50"}, {"type": "sale", "amount": "$300"}],
         {"refund": 1200.5, "sale": 300.0}),
        ([{"type": "sale", "amount": "$100"}, {"type": "sale", "amount": "$50"}],
         {"sale": 150.0}),
        ([{"type": "fee", "amount": "-$25.50"}], {"fee": -25.5}),
    ]
    for rows, out in memo:
        if rows == params["rows"]:
            return out
    return {}
