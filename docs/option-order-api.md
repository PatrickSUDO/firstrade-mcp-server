# Firstrade api3x options order endpoints (reverse-engineered)

Firstrade has no official public API or docs. Everything below was probed live with
`preview=true` (no real orders placed) using `tools/probe_option_codes.py` (single-leg
codes), `tools/probe_spread_endpoints.py` (endpoint/field discovery), and
`tools/probe_complex.py` (iterative field probing). Raw responses land in
`tools/probe-out/` (git-ignored — account numbers are masked, but it's still your own
live session output, so it isn't published here).

## Single leg — `POST /private/option_order`

| Field | Value |
|---|---|
| `transaction` | `BO` buy-to-open / `SO` sell-to-open / `SC` sell-to-close / `BC` buy-to-close (the upstream `firstrade` pip package only documents BO/SO; the other two work in practice) |
| `symbol` | OCC symbol, e.g. `NVDA261218C00400000` |
| `contracts` | int |
| `price_type` | `1` market / `2` limit / `3` stop / `4` stop-limit |
| `limit_price` / `stop_price` | depends on `price_type` |
| `duration` | `0` day / `1` GTC (>90 days) |
| `instructions` | `0` |
| `preview` | `"true"` / `"false"` |
| `account` | account number |

Common error codes: `1100` nothing to close (or you already have an open order on
this position); `1103` you hold a long position but sent `SO` (should be `SC`);
`1200` naked call write without 100 shares of the underlying (Level 2 margin
correctly rejecting it); `1562` broker-side rejection of opening orders (seen
intermittently in 2026-07, gone by 09-07 — cause unknown, may be a transient
server-side flag).

## Multi-leg spread — `POST /private/complex_option_order`

| Field | Value |
|---|---|
| `order_type` | `spread` (lowercase; other values like straddle/butterfly untested) |
| `transaction1` / `transaction2` | same codes as single-leg |
| `symbol1` / `symbol2` | OCC symbols |
| `contracts1` / `contracts2` | int |
| `limit_type` | `D` debit / `C` credit (the field validator literally returns "must be one of [C, D]") |
| `net_price` | float, net price for the whole combo |
| `instructions` | `0` (required) |
| `preview`, `account` | same as single-leg |

**Not accepted on this endpoint**: `duration`, `limit_price` (validator returns
"is not allowed") — multi-leg spread orders are day-only, no GTC.
Broker-side gate: only accepted 7am–4pm ET (otherwise `1110`; also rejected on
market-closed days).

## Validator behavior (useful for reverse-engineering further fields)

- Missing required field → `"X is required"`
- Unexpected field → `"Y is not allowed"`
- Bad enum value → `"must be one of […]"`

This makes the schema discoverable by iterating fields and reading the error message.
Both form-urlencoded and JSON request bodies are accepted.
