#!/usr/bin/env python3
"""
probe_option_codes.py — probes which `transaction` codes Firstrade's api3x
`option_order` endpoint actually accepts.

**Only ever sends preview=true — never places a real order.**

Background: the `firstrade` pip package (0.0.39) only exposes BO/SO as an
OrderType enum. The stock-order side has a BC code, so the option close codes
are very likely SC (sell-to-close) / BC (buy-to-close) too, but the package's
StrEnum blocks anything not in it before it's ever sent. This script bypasses
that enum and sends the raw payload directly through server.py's self-healing
session (same fields `place_option_order` builds internally).

Usage:
  cd firstrade-server && uv run python3 tools/probe_option_codes.py [--symbol OCC] [--price 0.05]
                          [--codes SC,BC,BO,SO] [--duration 0]

How to read the result (look at error/message, not just statusCode):
  SC/BC → "no position / not held / nothing to close"-style message ⇒ code is recognized, proceed to step 1b
  SC/BC → "invalid / unknown transaction / order type"-style message ⇒ close code isn't SC/BC, fold into the next round of probing
  BO    → still returns ref 1562 ⇒ the open-order issue is unrelated, hand off to the next step
Output: one summary line per code, plus the full JSON saved to
tools/probe-out/<date>.json (account number masked).
"""
import argparse
import json
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # firstrade-server/ → import server
import server  # noqa: E402  (mcp.run() only under __main__; import is side-effect free)
from firstrade import urls  # noqa: E402

DEFAULT_SYMBOL = "NVDA261218C00400000"   # far OTM, liquid, cheap; override with --symbol
OUT_DIR = os.path.join(HERE, "probe-out")


def mask(obj, acct):
    s = json.dumps(obj, ensure_ascii=False)
    return json.loads(s.replace(acct, acct[:2] + "****" + acct[-2:]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL, help="OCC option symbol")
    ap.add_argument("--price", type=float, default=0.05, help="limit price per contract")
    ap.add_argument("--contracts", type=int, default=1)
    ap.add_argument("--codes", default="SC,BC,BO,SO")
    ap.add_argument("--duration", default="0", help="0=day, 1=gt90")
    a = ap.parse_args()

    session, data = server._get_data()
    acct = data.account_numbers[0]
    results = {}
    print(f"probe {a.symbol} x{a.contracts} @ {a.price} limit, duration={a.duration}  (preview only)\n")
    print(f"{'code':5s} {'status':>6s}  error / message")
    for code in [c.strip().upper() for c in a.codes.split(",") if c.strip()]:
        payload = {
            "duration": a.duration,
            "instructions": "0",
            "transaction": code,
            "contracts": a.contracts,
            "symbol": a.symbol,
            "preview": "true",            # ← never "false" in this script
            "account": acct,
            "price_type": "2",            # LIMIT (firstrade.order.PriceType.LIMIT)
            "limit_price": a.price,
        }
        try:
            resp = session._request("post", url=urls.option_order(), data=payload)
            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text[:500]}
            status = resp.status_code
        except Exception as ex:  # noqa: BLE001
            body, status = {"exception": str(ex)}, -1
        results[code] = {"http": status, "response": body}
        err = body.get("error") if isinstance(body, dict) else None
        msg = body.get("message") if isinstance(body, dict) else None
        ref = ""
        if isinstance(body, dict):
            for k in ("ref", "reference", "code", "refcode"):
                if body.get(k):
                    ref = f" [ref {body[k]}]"
        print(f"{code:5s} {status:>6}  {err or ''} {('| ' + str(msg)) if msg else ''}{ref}")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"{date.today().isoformat()}.json")
    with open(out, "w") as f:
        json.dump({"symbol": a.symbol, "price": a.price, "results": mask(results, acct)}, f,
                  ensure_ascii=False, indent=1)
    print(f"\nfull responses → {out}")


if __name__ == "__main__":
    main()
