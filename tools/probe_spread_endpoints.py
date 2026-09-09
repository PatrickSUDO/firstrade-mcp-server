#!/usr/bin/env python3
"""
probe_spread_endpoints.py — guesses whether api3x has a dedicated multi-leg
(spread) order endpoint. **Only ever sends preview=true.**

How to read the result:
  404 / HTML                          → path doesn't exist
  400/422 + JSON reference code (incl. 1110 = time-window gate) → endpoint exists, fields can still be guessed
  200 + error==""                     → guessed the fields correctly (unlikely on a closed market day)
Output: one line per path x variant; full responses saved to
tools/probe-out/spread-<date>.json (account number masked).
"""
import json
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import server  # noqa: E402

BASE = "https://api3x.firstrade.com/private/"
PATHS = [
    "option_order",               # existing single-leg endpoint + a second-leg field
    "complex_option_order", "option_complex_order", "complex_order",
    "multileg_order", "multi_leg_order", "multileg_option_order",
    "spread_order", "option_spread", "option_spread_order",
    "strategy_order", "option_strategy_order",
]
LONG, SHORT = "NVDA261120C00270000", "NVDA261120C00280000"   # the two legs used by the web ticket UI


def variants(acct):
    common = {"duration": "0", "instructions": "0", "preview": "true", "account": acct,
              "price_type": "2", "limit_price": 0.10}
    flat = dict(common, transaction="BO", symbol=LONG, contracts=1,
                transaction2="SO", symbol2=SHORT, contracts2=1, net_price=0.10, credit_debit="D",
                strategy="spread")
    indexed = dict(common, **{
        "legs[0][transaction]": "BO", "legs[0][symbol]": LONG, "legs[0][contracts]": 1,
        "legs[1][transaction]": "SO", "legs[1][symbol]": SHORT, "legs[1][contracts]": 1,
        "net_price": 0.10, "credit_debit": "D"})
    as_json = dict(common, legs=[{"transaction": "BO", "symbol": LONG, "contracts": 1},
                                 {"transaction": "SO", "symbol": SHORT, "contracts": 1}],
                   net_price=0.10, credit_debit="D")
    return [("flat-form", {"data": flat}), ("indexed-form", {"data": indexed}), ("json", {"json": as_json})]


def main():
    session, data = server._get_data()
    acct = data.account_numbers[0]
    out = {}
    print(f"{'path':26s} {'variant':13s} {'http':>5s}  error / message")
    for p in PATHS:
        for name, kw in variants(acct):
            try:
                r = session._request("post", url=BASE + p, **kw)
                ct = r.headers.get("content-type", "")
                try:
                    body = r.json() if "json" in ct else {"raw": r.text[:300]}
                except Exception:
                    body = {"raw": r.text[:300]}
                status = r.status_code
            except Exception as ex:  # noqa: BLE001
                body, status = {"exception": str(ex)}, -1
            out[f"{p}|{name}"] = {"http": status, "body": body}
            if isinstance(body, dict) and "raw" in body:
                summary = "non-JSON: " + body["raw"][:60].replace("\n", " ")
            else:
                summary = f"{body.get('error','')} | {str(body.get('message',''))[:110]}"
            print(f"{p:26s} {name:13s} {status:>5}  {summary}")
    os.makedirs(os.path.join(HERE, "probe-out"), exist_ok=True)
    path = os.path.join(HERE, "probe-out", f"spread-{date.today().isoformat()}.json")
    with open(path, "w") as f:
        json.dump(json.loads(json.dumps(out).replace(acct, acct[:2] + "****" + acct[-2:])), f,
                  ensure_ascii=False, indent=1)
    print(f"\nfull → {path}")


if __name__ == "__main__":
    main()
