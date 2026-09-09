# firstrade-mcp-server

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives an LLM
(Claude, or any other MCP host) read/write access to a [Firstrade](https://www.firstrade.com)
brokerage account: live positions, balances, quotes, option chains/greeks, order
history, and — if you choose to enable it — order placement (stocks, single-leg
options, and two-leg option spreads).

Built on top of the community [`firstrade`](https://pypi.org/project/firstrade/)
Python package, which reverse-engineers Firstrade's internal `api3x` web API.
**This project is not affiliated with, endorsed by, or supported by Firstrade.**

## ⚠️ Read this before you use it

- Firstrade has no official public trading API. This server (like the `firstrade`
  package it depends on) works by driving the same private endpoints the Firstrade
  web app uses, authenticated with your real login. That is very likely outside
  the spirit — and possibly the letter — of Firstrade's Terms of Service around
  automated / unauthorized access. **Your account could be flagged, rate-limited,
  or suspended.** Use at your own risk, on an account you're prepared to lose access to.
- The order-placement tools (`place_stock_order`, `place_option_order`,
  `place_option_spread`) send **real orders with real money**. There is no
  simulated/paper mode. This is enforced server-side, not just by convention:
  `place_*` is disabled unless `FT_ALLOW_LIVE_ORDERS=true` is set in `.env`, and
  every call additionally requires a `confirm_token` minted by the matching
  `preview_*` tool for the *identical* order — a mismatched or missing token is
  rejected before anything is sent. See [Live order safety model](#live-order-safety-model).
- This is a personal tool the author built for their own workflow and is sharing
  as-is. It is not a product, has no support SLA, and comes with **no warranty of
  any kind** (see [LICENSE](LICENSE)). Nothing here is investment advice.
- Options trading requires an appropriately approved options level on your
  Firstrade account (e.g. naked calls need Level 2+ margin approval) — the broker
  enforces this server-side and will reject anything you're not approved for.

If any of that gives you pause, it should — read it twice before you put real
credentials in `.env`.

## What it does

| Tool | Purpose |
|---|---|
| `get_account_position` | Live stock + option positions, all accounts |
| `get_account_balance` | Equity, cash, buying power |
| `get_account_history` | Transaction history (fills, dividends, interest, transfers) — presets or a custom date range |
| `get_orders` | Open/filled/cancelled orders, with the Firstrade order id |
| `get_single_quote` / `get_watchlist_quote` | Real-time quote(s) |
| `get_option_chain` | Broker's own option chain; omit the expiration to list available expirations |
| `get_option_greeks` | Broker-computed delta/gamma/theta/vega/rho + IV for a chain |
| `preview_stock_order` / `place_stock_order` | Stock orders — buy, sell, sell_short, buy_to_cover; limit/market/stop/stop-limit/trailing |
| `preview_option_order` / `place_option_order` | Single-leg option orders — buy_to_open, sell_to_open, sell_to_close, buy_to_close |
| `preview_option_spread` / `place_option_spread` | Two-leg option spreads (debit or credit), priced by net price |
| `cancel_order` | Cancel an open order by id |

Every `place_*` tool has a matching `preview_*` tool that runs the identical
request in dry-run mode. The intended usage pattern for an LLM host is:
**always preview first, show the user the preview, only place after explicit
confirmation** — and the server enforces this, it doesn't just document it (see
below).

## Live order safety model

`place_stock_order`, `place_option_order`, and `place_option_spread` are gated
by two independent checks, both server-side:

1. **Kill switch.** They refuse to run unless `FT_ALLOW_LIVE_ORDERS=true` is set
   in `.env`. Unset (the default) or anything else, and every `place_*` call
   returns an error without touching the network — `preview_*` still works, so
   you can wire this up and see previews before ever flipping the switch.
2. **Preview→place confirmation token.** Every `preview_*` call mints a one-time
   `confirm_token` bound to the exact order arguments (symbol, side, quantity,
   price, duration, etc.), valid for 10 minutes. The matching `place_*` call
   must pass that token back unchanged. A missing token, an expired token, or a
   token minted for *different* order arguments (e.g. the LLM previewed 10
   shares but tries to place 100) is rejected before the order reaches
   Firstrade. Tokens live in-process only — a server restart invalidates every
   pending preview.

This closes the gap where "preview first" was only a docstring instruction an
LLM host could skip or a permissions layer could bypass; now placing an order
that was never (or differently) previewed is impossible at the code level.

Two more things worth knowing:
- `duration` on stock orders defaults to `gt90` (Firstrade's ~90-day GTC), which
  the confirm-token flow forces you to see in the preview before it can be sent.
  Pass `duration="day"` explicitly if you don't want a resting GTC order.
- If your login has more than one Firstrade account, order/quote/cancel tools
  refuse to guess which one you mean — set `FT_ACCOUNT_NUMBER` in `.env`.

See [`docs/option-order-api.md`](docs/option-order-api.md) for the reverse-engineered
schema of Firstrade's single-leg and multi-leg option order endpoints (error
codes, field validation behavior, GTC vs day-only constraints), discovered via
the probe scripts in [`tools/`](tools/).

## Prerequisites

- Python **3.12+**
- [uv](https://github.com/astral-sh/uv) (recommended; plain `pip install -e .` also works)
- A Firstrade account, and an authenticator app if you want headless session refresh (see below)

## Setup

```bash
git clone https://github.com/PatrickSUDO/firstrade-mcp-server.git
cd firstrade-mcp-server
uv sync
cp .env.example .env
# edit .env: fill in FT_USERNAME, FT_PASSWORD, FT_PIN, FT_EMAIL
# leave FT_ALLOW_LIVE_ORDERS unset until you've reviewed the safety model below
```

### First login (interactive)

Firstrade requires 2FA (OTP or authenticator MFA) on every fresh login. Run this
once to establish a session:

```bash
uv run python3 tools/ft_setup.py step1
# → sends an OTP / prompts for your authenticator code
uv run python3 tools/ft_setup.py step2 <CODE>
```

This saves session cookies to `~/.local/share/firstrade-session`. `server.py`
reuses that saved session on every tool call, so you don't re-auth per request.

### Optional: headless session refresh

Firstrade sessions expire periodically. If you set `FT_TOTP_SECRET` in `.env`
(the seed your authenticator app was set up with — not the 6-digit code), the
server self-heals automatically: it detects a dead session (401) and re-runs
the login + TOTP flow without a human typing anything. You can also trigger
this manually:

```bash
uv run python3 tools/ft_setup.py auto
```

Without `FT_TOTP_SECRET`, a dead session falls back to the manual `step1` /
`step2 <code>` flow above.

## Register with an MCP host

**Claude Code:**

```bash
claude mcp add firstrade-server -- uv --directory /absolute/path/to/firstrade-mcp-server run server.py
```

**Generic `mcp.json` / host config:**

```json
{
  "mcpServers": {
    "firstrade-server": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/firstrade-mcp-server", "run", "server.py"]
    }
  }
}
```

Credentials are read from `firstrade-server/.env` at startup — you don't need
to (and shouldn't) put them in the MCP host config.

## Security notes

- No credentials are hardcoded anywhere in the source. `server.py` and
  `tools/ft_setup.py` both load `FT_*` values from a local `.env` file that is
  git-ignored.
- `FT_TOTP_SECRET`, if you set it, is your authenticator's full seed — not a
  6-digit code. Anyone with it (or with your `.env`) can mint valid login codes
  for your account indefinitely, no phone required. Treat `.env` like a
  password, not a config file: `chmod 600` it, don't sync it anywhere shared.
- The saved session (`~/.local/share/firstrade-session`) and the transient
  login-flow state (`~/.local/share/firstrade-session-tmp/`) hold live cookies
  / tokens. Both are written with `0600`/`0700` perms and kept out of `/tmp`
  (which is world-readable on most multi-user machines). `ft_setup.py` also
  never prints raw tokens/cookies to stdout — only redacted shapes — since a
  failed headless re-auth surfaces its tail through the MCP error channel.
- `uv.lock` is committed and `firstrade` is pinned to an exact version, not a
  floating `>=`. This wraps an unofficial, reverse-engineered API client that
  runs against a live brokerage account — bump it deliberately, after testing,
  not automatically on `uv sync`.
- `tools/probe-out/` (raw API responses captured while reverse-engineering the
  option order schema) is git-ignored too — even though account numbers in
  those responses are masked by Firstrade itself, it's still your own live
  session output.
- Review `.gitignore` before you fork/extend this and make sure your own `.env`,
  session cache, and any new debug-output directories stay out of git.

## License

[MIT](LICENSE). Provided as-is, for personal/educational use. Not investment
advice. Not affiliated with Firstrade.
