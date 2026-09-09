#!/usr/bin/env python3
"""
probe_complex.py — iteratively guesses the field schema of api3x
/private/complex_option_order. **Always preview=true.**

Usage: each CLI arg is a JSON dict, merged onto a base payload; --json sends a
JSON body instead of the default form-urlencoded.
  uv run python3 tools/probe_complex.py '{"order_type":"spread"}' '{"order_type":"1"}'
api3x's validator replies "X is required" / "Y is not allowed" — fill in fields
based on those messages until it accepts the request.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

URL = "https://api3x.firstrade.com/private/complex_option_order"


def main():
    args = [a for a in sys.argv[1:] if a != "--json"]
    use_json = "--json" in sys.argv
    session, data = server._get_data()
    acct = data.account_numbers[0]
    base = {"preview": "true", "account": acct}
    for a in args:
        payload = dict(base, **json.loads(a))
        kw = {"json": payload} if use_json else {"data": payload}
        r = session._request("post", url=URL, **kw)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:300]}
        shown = {k: v for k, v in payload.items() if k not in ("account", "preview")}
        msg = body.get("message") if isinstance(body, dict) else body
        print(f"{r.status_code}  {json.dumps(shown, ensure_ascii=False)}\n     → {body.get('error','') if isinstance(body, dict) else ''} | {str(msg)[:400]}")
        if isinstance(body, dict) and body.get("result"):
            print("     result:", json.dumps(body["result"], ensure_ascii=False)[:600])


if __name__ == "__main__":
    sys.path.insert(0, ".")
    main()
