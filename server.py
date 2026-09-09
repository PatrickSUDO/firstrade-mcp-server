import os
import re
import sys
import json
import time
import uuid
import stat
import subprocess
from mcp.server.fastmcp import FastMCP
from firstrade.account import FTSession, FTAccountData


def _load_env():
    """Load FT_* credentials from server-dir .env (stdlib only, no commit risk)."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    v = v.split("#")[0].strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                        v = v[1:-1]
                    os.environ.setdefault(k.strip(), v)


_load_env()

mcp = FastMCP("firstrade-server")

USERNAME = os.environ.get("FT_USERNAME", "")
PASSWORD = os.environ.get("FT_PASSWORD", "")
PIN      = os.environ.get("FT_PIN", "")
EMAIL    = os.environ.get("FT_EMAIL", "")
TOTP_SECRET = os.environ.get("FT_TOTP_SECRET", "")  # enables headless self-heal
ACCOUNT_OVERRIDE = os.environ.get("FT_ACCOUNT_NUMBER", "").strip()  # required if >1 account
ALLOW_LIVE_ORDERS = os.environ.get("FT_ALLOW_LIVE_ORDERS", "").strip().lower() in ("1", "true", "yes")
PROFILE  = os.path.expanduser("~/.local/share/firstrade-session")
_FT_SETUP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "ft_setup.py")

_session: FTSession | None = None
_data: FTAccountData | None = None

# ── Preview→place confirmation tokens ───────────────────────────────────────
# A place_* call must reference a token minted by the matching preview_* call for
# the *exact same* order (same tool, same canonicalized args). This turns "only
# call place_* after the user confirms the preview" from a docstring convention
# an LLM host can skip into something the server itself enforces. Tokens are
# single-use and expire after 10 minutes; in-memory only (process-lifetime), so
# a server restart invalidates all pending previews — that's intentional.
_PREVIEW_TTL_SECONDS = 600
_previews: dict[str, tuple[str, str]] = {}  # token -> (tool_name, canonical_args_json)


def _canon_args(**kwargs) -> str:
    return json.dumps(kwargs, sort_keys=True, default=str)


def _mint_preview_token(tool_name: str, **kwargs) -> str:
    now = time.time()
    # opportunistic sweep of expired tokens so the dict doesn't grow unbounded
    for tok in [t for t, (exp, _) in _previews.items() if now > float(exp)]:
        del _previews[tok]
    token = uuid.uuid4().hex
    _previews[token] = (str(now + _PREVIEW_TTL_SECONDS), f"{tool_name}:{_canon_args(**kwargs)}")
    return token


def _check_preview_token(tool_name: str, token: str, **kwargs) -> str | None:
    """Returns an error string if the token is missing/expired/mismatched, else None
    (and consumes the token — one preview authorizes exactly one place)."""
    entry = _previews.pop(token, None) if token else None
    if entry is None:
        return (
            "No matching preview found for confirm_token. Call the matching preview_* "
            "tool first with the exact same arguments, then pass the token it returns "
            "as confirm_token."
        )
    expiry_s, recorded = entry
    if time.time() > float(expiry_s):
        return "confirm_token expired (previews are valid for 10 minutes) — preview again."
    expected = f"{tool_name}:{_canon_args(**kwargs)}"
    if recorded != expected:
        return "confirm_token was minted for different order arguments — preview again with the exact order you intend to place."
    return None


def _require_live_orders_enabled() -> str | None:
    if not ALLOW_LIVE_ORDERS:
        return (
            "Live order placement is disabled. Set FT_ALLOW_LIVE_ORDERS=true in "
            "firstrade-server/.env to enable place_* tools. This is an explicit "
            "opt-in kill switch — leave it off unless you intend to send real orders."
        )
    return None


_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_\-\.]{24,}")


def _scrub(text: str) -> str:
    """Mask long token/cookie-shaped substrings before any subprocess output is
    surfaced through the MCP error channel (session tokens, ftat, sid, cookies)."""
    return _SECRET_PATTERN.sub(lambda m: m.group(0)[:4] + "…redacted…" + m.group(0)[-4:], text)


def _select_account(data: FTAccountData) -> tuple[str | None, str | None]:
    """Pick the account order/quote/cancel tools operate on. Never silently default
    to accounts[0] when more than one account exists — that's a fat-finger risk on
    a multi-account login. Returns (acct, error_json); exactly one is non-None."""
    if not data.account_numbers:
        return None, json.dumps({"error": "No account found"})
    if ACCOUNT_OVERRIDE:
        if ACCOUNT_OVERRIDE not in data.account_numbers:
            return None, json.dumps({
                "error": f"FT_ACCOUNT_NUMBER={ACCOUNT_OVERRIDE!r} not found among accounts",
                "accounts": data.account_numbers,
            })
        return ACCOUNT_OVERRIDE, None
    if len(data.account_numbers) > 1:
        return None, json.dumps({
            "error": "Multiple Firstrade accounts found and FT_ACCOUNT_NUMBER is not set.",
            "accounts": data.account_numbers,
            "fix": "Set FT_ACCOUNT_NUMBER in firstrade-server/.env to the account these tools should use.",
        })
    return data.account_numbers[0], None


_REFRESH_HINT = (
    "Firstrade session expired and headless self-heal is unavailable.\n"
    "Set FT_TOTP_SECRET in firstrade-server/.env for auto-refresh, or run:\n"
    "  cd <firstrade-server> && uv run python3 tools/ft_setup.py auto\n"
    "then /mcp reconnect firstrade-server."
)


def _session_live(data: FTAccountData) -> bool:
    """Cheap authenticated probe: a saved cookie can be reused by login() yet be
    server-side expired (API then returns 401 'Blank or invalid session'). Detect
    that here so we never cache a dead session and 401 forever."""
    try:
        for acct in data.account_numbers:
            bal = data.get_account_balances(acct)
            # firstrade returns the 401 body as the payload rather than raising
            if isinstance(bal, dict) and bal.get("statusCode") == 401:
                return False
            return True  # first account answered without 401 → session live
        return bool(data.account_numbers)  # no accounts to probe → trust login()
    except Exception:
        return True  # transient/unknown error: don't nuke the session over it


def _auto_mint() -> None:
    """Headless re-auth via TOTP, reusing the tested ft_setup.py `auto` flow as a
    subprocess. Subprocess (not import) so a failure can't sys.exit-kill this
    server, and capture_output keeps its stdout/prints from corrupting the MCP
    stdio protocol stream. Rewrites the saved session at PROFILE on success."""
    if not TOTP_SECRET:
        raise RuntimeError("Firstrade session dead and FT_TOTP_SECRET not set.\n" + _REFRESH_HINT)
    r = subprocess.run(
        [sys.executable, _FT_SETUP, "auto"],
        capture_output=True, text=True, timeout=120,
        cwd=os.path.dirname(_FT_SETUP),
    )
    if r.returncode != 0:
        tail = _scrub((r.stderr or r.stdout or "")[-600:])
        raise RuntimeError(f"Firstrade auto re-auth failed (exit {r.returncode}).\n{tail}\n" + _REFRESH_HINT)


def _harden_profile_perms() -> None:
    """Best-effort: the saved session (cookies/tokens) should not be group/world
    readable on a shared machine. The `firstrade` package controls how PROFILE is
    written, so we just tighten perms after the fact; failures are non-fatal."""
    try:
        if os.path.exists(PROFILE):
            os.chmod(PROFILE, stat.S_IRUSR | stat.S_IWUSR)
        parent = os.path.dirname(PROFILE)
        if parent and os.path.exists(parent):
            os.chmod(parent, stat.S_IRWXU)
    except OSError:
        pass


def _build_session() -> tuple[FTSession | None, FTAccountData | None]:
    """Load a session from the saved profile. Returns (None, None) whenever the
    saved session can't be reused, so the caller falls through to _auto_mint().
    login() may either return True (OTP required) OR raise LoginResponseError
    ('Bad Request') when there's no reusable saved session — handle both."""
    try:
        session = FTSession(
            username=USERNAME, password=PASSWORD, pin=PIN, email=EMAIL,
            profile_path=PROFILE, save_session=True,
        )
        if session.login():  # True → OTP required / no reusable saved session
            return None, None
        _harden_profile_perms()
        return session, FTAccountData(session)
    except Exception:
        return None, None    # any failure building from saved session → re-mint


def _get_data() -> tuple[FTSession, FTAccountData]:
    """Return a live (session, data), self-healing a server-side-expired session.

    Fixes the recurring mid-session 401: a saved cookie can pass login() yet be
    server-side dead, and previously a cached _session was never re-checked so it
    401'd forever. Now we (1) re-probe liveness on every call and drop a dead
    cache, and (2) auto re-auth via TOTP when the saved session is invalid — no
    manual ft_setup or /mcp reconnect needed mid-session."""
    global _session, _data
    # Cached session: verify still live; drop it and re-auth if server-side dead.
    if _session is not None:
        if _session_live(_data):
            return _session, _data
        _session, _data = None, None

    session, data = _build_session()
    if data is None or not _session_live(data):
        _auto_mint()                       # headless TOTP refresh → rewrites PROFILE
        session, data = _build_session()
        if data is None or not _session_live(data):
            raise RuntimeError("Firstrade session invalid after TOTP self-heal.\n" + _REFRESH_HINT)
    _session, _data = session, data
    return _session, _data


@mcp.tool()
def get_account_position() -> str:
    """Get current stock and options positions for all accounts."""
    _, data = _get_data()
    result = {}
    for acct in data.account_numbers:
        result[acct] = data.get_positions(acct)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def get_account_balance() -> str:
    """Get account equity, cash, and balance overview for all accounts."""
    _, data = _get_data()
    result = {}
    for acct in data.account_numbers:
        result[acct] = data.get_account_balances(acct)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def get_account_history(
    date_range: str = "1m",
    custom_from: str = "",
    custom_to: str = "",
) -> str:
    """Get transaction history (fills, dividends, interest, transfers).

    Args:
        date_range: today|1w|1m|2m|mtd|ytd|ly|cust. Use "ly" for the trailing
            year, or "cust" with custom_from/custom_to for an explicit window.
        custom_from: Window start, "YYYY-MM-DD". Required when date_range="cust";
            supplying it also implies "cust" so the range arg can be left alone.
        custom_to: Window end, "YYYY-MM-DD". Defaults to custom_from when omitted.

    Returns JSON keyed by account number.
    """
    _, data = _get_data()
    custom_range = None
    if custom_from:
        custom_range = [custom_from, custom_to or custom_from]
        date_range = "cust"
    elif date_range == "cust":
        return json.dumps(
            {"error": "date_range='cust' requires custom_from (YYYY-MM-DD)"},
            ensure_ascii=False,
        )
    result = {}
    for acct in data.account_numbers:
        result[acct] = data.get_account_history(
            acct, date_range=date_range, custom_range=custom_range
        )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def get_orders(per_page: int = 0) -> str:
    """List orders and their status (open / filled / cancelled) for all accounts.

    Each entry carries the Firstrade order id (e.g. 'G42621-1601'), which is the
    authoritative link between a fill and the GTC ladder it came from. Use this to
    see resting GTC orders, detect dead orders, and attribute fills to a plan.

    Args:
        per_page: Orders per page. 0 (default) returns all.

    Returns JSON keyed by account number.
    """
    _, data = _get_data()
    result = {}
    for acct in data.account_numbers:
        result[acct] = data.get_orders(acct, per_page=per_page)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def get_single_quote(symbol: str) -> str:
    """Get real-time quote for a stock symbol."""
    session, data = _get_data()
    acct, err = _select_account(data)
    if err:
        return err
    from firstrade import urls
    resp = session._request("get", urls.quote(acct, symbol))
    return json.dumps(resp.json(), ensure_ascii=False)


@mcp.tool()
def get_watchlist_quote(symbols: str) -> str:
    """Get real-time quotes for multiple symbols (comma-separated, e.g. 'AAPL,NVDA,MU')."""
    session, data = _get_data()
    acct, err = _select_account(data)
    if err:
        return err
    from firstrade import urls
    results = {}
    for sym in [s.strip() for s in symbols.split(",")]:
        resp = session._request("get", urls.quote(acct, sym))
        results[sym] = resp.json()
    return json.dumps(results, ensure_ascii=False)


def _exp_date(raw: str) -> str:
    """Firstrade's option endpoints want YYYYMMDD; accept YYYY-MM-DD too."""
    return raw.replace("-", "").strip()


@mcp.tool()
def get_option_chain(symbol: str, exp_date: str = "") -> str:
    """Get the broker's own option chain for a stock symbol.

    Args:
        symbol: Underlying ticker, e.g. 'NVDA'.
        exp_date: Expiration as 'YYYYMMDD' (or 'YYYY-MM-DD'). Omit to list the
            available expiration dates instead of returning a chain.

    Returns JSON: {"items": [{exp_date, day_left, exp_type}, ...]} when exp_date is
    omitted, else the chain for that expiration.
    """
    session, _ = _get_data()
    from firstrade import urls
    if not exp_date:
        resp = session._request("get", url=urls.option_dates(symbol))
    else:
        resp = session._request("get", url=urls.option_quotes(symbol, _exp_date(exp_date)))
    return json.dumps(resp.json(), ensure_ascii=False)


@mcp.tool()
def get_option_greeks(symbol: str, exp_date: str) -> str:
    """Get broker-computed greeks (delta/gamma/theta/vega/rho, IV) for an option chain.

    Prefer this over locally derived greeks when sizing or comparing legs.

    Args:
        symbol: Underlying ticker, e.g. 'TSLA'.
        exp_date: Expiration as 'YYYYMMDD' (or 'YYYY-MM-DD'). Get valid dates from
            get_option_chain with exp_date omitted.

    Returns JSON {"chains": [{strike, cp, side, symbol, iv, delta, gamma, rho,
    theta, vega}, ...]}. Illiquid strikes report "--" rather than a number.
    """
    session, _ = _get_data()
    from firstrade import urls
    payload = {
        "type": "chain",
        "chains_range": "A",
        "root_symbol": symbol,
        "exp_date": _exp_date(exp_date),
    }
    resp = session._request("post", url=urls.greek_options(), data=payload)
    return json.dumps(resp.json(), ensure_ascii=False)


# ── Order tools ──────────────────────────────────────────────────────────────
# Safety model: preview_* tools use dry_run=True (no real order sent).
# place_* tools use dry_run=False and must only be called after the user
# explicitly confirms the preview result.

_PRICE_TYPES = {
    "limit": "2",
    "market": "1",
    "stop": "3",
    "stop_limit": "4",
    "trailing_stop_dollar": "5",
    "trailing_stop_percent": "6",
}

_ORDER_TYPES_STOCK = {
    "buy": "B",
    "sell": "S",
    "sell_short": "SS",
    "buy_to_cover": "BC",
}

# Firstrade api3x option transaction codes. The upstream `firstrade` OrderType enum
# only lists BO/SO, but the API accepts the close codes too — verified 2026-09-07 via
# preview-only probes (tools/probe_option_codes.py): SC/BC answer with position-based
# errors (ref 1100 "insufficient available shares"), not "invalid transaction", and
# BO previews 200/Normal. We therefore send the raw code ourselves (_raw_option_order)
# instead of going through the enum-guarded Order.place_option_order().
_ORDER_TYPES_OPTION = {
    "buy": "BO",             # alias of buy_to_open (kept for backward compatibility)
    "sell": "SO",            # alias of sell_to_open (naked/covered write; ref 1200 if uncovered)
    "buy_to_open": "BO",
    "sell_to_open": "SO",
    "sell_to_close": "SC",   # close a long option
    "buy_to_close": "BC",    # close a short option
}

_DURATIONS = {
    "day": "0",
    "day_ext": "D",
    "overnight": "N",
    "gt90": "1",
}


def _stock_order(
    symbol: str,
    order_type: str,
    quantity: int,
    price_type: str,
    duration: str,
    price: float,
    stop_price: float | None,
    dry_run: bool,
) -> str:
    session, data = _get_data()
    acct, err = _select_account(data)
    if err:
        return err

    ot = _ORDER_TYPES_STOCK.get(order_type.lower())
    if ot is None:
        return json.dumps({"error": f"Unknown order_type '{order_type}'. Valid: {list(_ORDER_TYPES_STOCK)}"})
    pt = _PRICE_TYPES.get(price_type.lower())
    if pt is None:
        return json.dumps({"error": f"Unknown price_type '{price_type}'. Valid: {list(_PRICE_TYPES)}"})
    dur = _DURATIONS.get(duration.lower())
    if dur is None:
        return json.dumps({"error": f"Unknown duration '{duration}'. Valid: {list(_DURATIONS)}"})

    from firstrade.order import Order, PriceType, OrderType, Duration
    order = Order(session)
    result = order.place_order(
        account=acct,
        symbol=symbol.upper(),
        price_type=PriceType(pt),
        order_type=OrderType(ot),
        duration=Duration(dur),
        quantity=quantity,
        price=price,
        stop_price=stop_price,
        dry_run=dry_run,
    )
    return json.dumps(result, ensure_ascii=False)


def _option_order(
    option_symbol: str,
    order_type: str,
    contracts: int,
    price_type: str,
    duration: str,
    price: float,
    stop_price: float | None,
    dry_run: bool,
) -> str:
    session, data = _get_data()
    acct, err = _select_account(data)
    if err:
        return err

    ot = _ORDER_TYPES_OPTION.get(order_type.lower())
    if ot is None:
        return json.dumps({"error": f"Unknown order_type '{order_type}'. Valid: {list(_ORDER_TYPES_OPTION)}"})
    pt = _PRICE_TYPES.get(price_type.lower())
    if pt is None:
        return json.dumps({"error": f"Unknown price_type '{price_type}'. Valid: {list(_PRICE_TYPES)}"})
    dur = _DURATIONS.get(duration.lower())
    if dur is None:
        return json.dumps({"error": f"Unknown duration '{duration}'. Valid: {list(_DURATIONS)}"})

    result = _raw_option_order(
        session, acct, option_symbol.upper(), ot, contracts, pt, dur, price, stop_price, dry_run
    )
    return json.dumps(result, ensure_ascii=False)


def _raw_option_order(
    session,
    acct: str,
    option_symbol: str,
    transaction: str,
    contracts: int,
    price_type_code: str,
    duration_code: str,
    price: float,
    stop_price: float | None,
    dry_run: bool,
) -> dict:
    """Mirror of firstrade.order.Order.place_option_order, minus the OrderType enum
    guard, so close codes (SC/BC) can be sent. Same endpoint, same payload shape:
    preview first; only when dry_run=False is the order re-posted with preview=false.
    Not patching site-packages keeps this survivable across `uv sync`."""
    from firstrade import urls
    from firstrade.order import PriceType

    data = {
        "duration": duration_code,
        "instructions": "0",
        "transaction": transaction,
        "contracts": contracts,
        "symbol": option_symbol,
        "preview": "true",
        "account": acct,
        "price_type": price_type_code,
    }
    if price_type_code in {PriceType.LIMIT, PriceType.STOP_LIMIT}:
        data["limit_price"] = price
    if price_type_code in {PriceType.STOP, PriceType.STOP_LIMIT}:
        data["stop_price"] = stop_price

    resp = session._request("post", url=urls.option_order(), data=data)
    body = resp.json()
    if resp.status_code != 200 or body.get("error"):
        return body
    if dry_run:
        return body
    data["preview"] = "false"
    resp = session._request("post", url=urls.option_order(), data=data)
    return resp.json()


@mcp.tool()
def preview_stock_order(
    symbol: str,
    order_type: str,
    quantity: int,
    price_type: str = "limit",
    duration: str = "gt90",
    price: float = 0.0,
    stop_price: float | None = None,
) -> str:
    """Preview a stock order WITHOUT sending it (dry_run=True). Always call this first.

    Args:
        symbol: Ticker symbol (e.g. 'NVDA').
        order_type: buy | sell | sell_short | buy_to_cover
        quantity: Number of shares.
        price_type: limit | market | stop | stop_limit | trailing_stop_dollar | trailing_stop_percent
        duration: day | day_ext | overnight | gt90 (gt90 ≈ GTC, 90-day)
        price: Limit price (required for limit/stop_limit orders).
        stop_price: Stop trigger price (required for stop/stop_limit orders).

    Returns JSON with order preview confirmation data, plus "confirm_token": pass
    that token unchanged to place_stock_order (with the identical order arguments)
    to actually send it. The token expires in 10 minutes and works once.
    """
    result = json.loads(_stock_order(symbol, order_type, quantity, price_type, duration, price, stop_price, dry_run=True))
    if isinstance(result, dict) and not result.get("error"):
        result["confirm_token"] = _mint_preview_token(
            "stock", symbol=symbol, order_type=order_type, quantity=quantity,
            price_type=price_type, duration=duration, price=price, stop_price=stop_price,
        )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def place_stock_order(
    symbol: str,
    order_type: str,
    quantity: int,
    confirm_token: str,
    price_type: str = "limit",
    duration: str = "gt90",
    price: float = 0.0,
    stop_price: float | None = None,
) -> str:
    """Place a real stock order (dry_run=False). Requires FT_ALLOW_LIVE_ORDERS=true in
    .env AND a confirm_token from preview_stock_order called with these exact same
    arguments — the server rejects the order otherwise, it does not just rely on the
    caller having "meant to" preview first.

    Args:
        symbol: Ticker symbol (e.g. 'NVDA').
        order_type: buy | sell | sell_short | buy_to_cover
        quantity: Number of shares.
        confirm_token: Token returned by preview_stock_order for this exact order.
        price_type: limit | market | stop | stop_limit | trailing_stop_dollar | trailing_stop_percent
        duration: day | day_ext | overnight | gt90 (gt90 ≈ GTC, 90-day)
        price: Limit price (required for limit/stop_limit orders).
        stop_price: Stop trigger price (required for stop/stop_limit orders).

    Returns JSON with order confirmation. This sends a real order to Firstrade.
    """
    gate_err = _require_live_orders_enabled()
    if gate_err:
        return json.dumps({"error": gate_err})
    tok_err = _check_preview_token(
        "stock", confirm_token, symbol=symbol, order_type=order_type, quantity=quantity,
        price_type=price_type, duration=duration, price=price, stop_price=stop_price,
    )
    if tok_err:
        return json.dumps({"error": tok_err})
    return _stock_order(symbol, order_type, quantity, price_type, duration, price, stop_price, dry_run=False)


@mcp.tool()
def preview_option_order(
    option_symbol: str,
    order_type: str,
    contracts: int,
    price_type: str = "limit",
    duration: str = "day",
    price: float = 0.0,
    stop_price: float | None = None,
) -> str:
    """Preview an option order WITHOUT sending it (dry_run=True). Always call this first.

    Args:
        option_symbol: OCC format symbol (e.g. 'AAPL250620C00150000').
        order_type: buy_to_open | sell_to_close | sell_to_open | buy_to_close
            ('buy' = buy_to_open, 'sell' = sell_to_open; to exit a long option you
            MUST use sell_to_close, otherwise Firstrade treats it as opening a short
            and rejects with ref 1103).
        contracts: Number of contracts.
        price_type: limit | market | stop | stop_limit
        duration: day | day_ext | gt90
        price: Limit price per contract (required for limit orders).
        stop_price: Stop trigger price (required for stop/stop_limit orders).

    Returns JSON with order preview confirmation data, plus "confirm_token": pass
    that token unchanged to place_option_order (with the identical order arguments)
    to actually send it. The token expires in 10 minutes and works once.
    """
    result = json.loads(_option_order(option_symbol, order_type, contracts, price_type, duration, price, stop_price, dry_run=True))
    if isinstance(result, dict) and not result.get("error"):
        result["confirm_token"] = _mint_preview_token(
            "option", option_symbol=option_symbol, order_type=order_type, contracts=contracts,
            price_type=price_type, duration=duration, price=price, stop_price=stop_price,
        )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def place_option_order(
    option_symbol: str,
    order_type: str,
    contracts: int,
    confirm_token: str,
    price_type: str = "limit",
    duration: str = "day",
    price: float = 0.0,
    stop_price: float | None = None,
) -> str:
    """Place a real option order (dry_run=False). Requires FT_ALLOW_LIVE_ORDERS=true in
    .env AND a confirm_token from preview_option_order called with these exact same
    arguments — the server rejects the order otherwise, it does not just rely on the
    caller having "meant to" preview first.

    Args:
        option_symbol: OCC format symbol (e.g. 'AAPL250620C00150000').
        order_type: buy_to_open | sell_to_close | sell_to_open | buy_to_close
            ('buy' = buy_to_open, 'sell' = sell_to_open; to exit a long option you
            MUST use sell_to_close, otherwise Firstrade treats it as opening a short
            and rejects with ref 1103).
        contracts: Number of contracts.
        confirm_token: Token returned by preview_option_order for this exact order.
        price_type: limit | market | stop | stop_limit
        duration: day | day_ext | gt90
        price: Limit price per contract (required for limit orders).
        stop_price: Stop trigger price (required for stop/stop_limit orders).

    Returns JSON with order confirmation. This sends a real order to Firstrade.
    """
    gate_err = _require_live_orders_enabled()
    if gate_err:
        return json.dumps({"error": gate_err})
    tok_err = _check_preview_token(
        "option", confirm_token, option_symbol=option_symbol, order_type=order_type, contracts=contracts,
        price_type=price_type, duration=duration, price=price, stop_price=stop_price,
    )
    if tok_err:
        return json.dumps({"error": tok_err})
    return _option_order(option_symbol, order_type, contracts, price_type, duration, price, stop_price, dry_run=False)


_LIMIT_TYPES_SPREAD = {"debit": "D", "credit": "C", "d": "D", "c": "C"}


def _raw_spread_order(
    session,
    acct: str,
    leg1: tuple[str, str, int],
    leg2: tuple[str, str, int],
    limit_type_code: str,
    net_price: float,
    dry_run: bool,
) -> dict:
    """POST api3x /private/complex_option_order. Schema discovered 2026-09-07 by
    iterating the API's own validator messages (tools/probe_complex.py):
      order_type=spread, transaction{1,2}, symbol{1,2}, contracts{1,2},
      limit_type in {C, D}, net_price, instructions, preview, account.
    `duration` and `limit_price` are rejected ("not allowed") — complex orders are
    day-only and priced by net_price. Broker gate: 7AM–4PM ET only (ref 1110)."""
    from firstrade import urls

    data = {
        "order_type": "spread",
        "transaction1": leg1[0], "symbol1": leg1[1], "contracts1": leg1[2],
        "transaction2": leg2[0], "symbol2": leg2[1], "contracts2": leg2[2],
        "limit_type": limit_type_code,
        "net_price": net_price,
        "instructions": "0",
        "preview": "true",
        "account": acct,
    }
    url = urls.option_order().replace("option_order", "complex_option_order")
    resp = session._request("post", url=url, data=data)
    body = resp.json()
    if resp.status_code != 200 or body.get("error"):
        return body
    if dry_run:
        return body
    data["preview"] = "false"
    resp = session._request("post", url=url, data=data)
    return resp.json()


def _spread_order(
    symbol1: str, transaction1: str, contracts1: int,
    symbol2: str, transaction2: str, contracts2: int,
    limit_type: str, net_price: float, dry_run: bool,
) -> str:
    session, data = _get_data()
    acct, err = _select_account(data)
    if err:
        return err
    t1 = _ORDER_TYPES_OPTION.get(transaction1.lower())
    t2 = _ORDER_TYPES_OPTION.get(transaction2.lower())
    if t1 is None or t2 is None:
        return json.dumps({"error": f"Unknown leg transaction. Valid: {list(_ORDER_TYPES_OPTION)}"})
    lt = _LIMIT_TYPES_SPREAD.get(limit_type.lower())
    if lt is None:
        return json.dumps({"error": "limit_type must be 'debit' or 'credit'"})
    result = _raw_spread_order(
        session, acct,
        (t1, symbol1.upper(), contracts1), (t2, symbol2.upper(), contracts2),
        lt, net_price, dry_run,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def preview_option_spread(
    symbol1: str,
    transaction1: str,
    symbol2: str,
    transaction2: str,
    limit_type: str,
    net_price: float,
    contracts1: int = 1,
    contracts2: int = 1,
) -> str:
    """Preview a two-leg option spread WITHOUT sending it (dry_run=True). Always call this first.

    Args:
        symbol1 / symbol2: OCC symbols of the two legs (e.g. 'NVDA261120C00270000').
        transaction1 / transaction2: buy_to_open | sell_to_open | sell_to_close | buy_to_close per leg
            (e.g. debit call spread = leg1 buy_to_open lower strike, leg2 sell_to_open higher strike).
        limit_type: 'debit' (you pay net_price) or 'credit' (you receive net_price).
        net_price: Net limit price per spread.
        contracts1 / contracts2: Contracts per leg (default 1 each).

    Notes: complex orders are DAY only (no GTC) and accepted by Firstrade only 7AM–4PM ET
    (ref 1110 otherwise). Returns JSON preview plus "confirm_token": pass that token
    unchanged to place_option_spread (with the identical arguments) to actually send
    it. The token expires in 10 minutes and works once.
    """
    result = json.loads(_spread_order(symbol1, transaction1, contracts1, symbol2, transaction2, contracts2,
                         limit_type, net_price, dry_run=True))
    if isinstance(result, dict) and not result.get("error"):
        result["confirm_token"] = _mint_preview_token(
            "spread", symbol1=symbol1, transaction1=transaction1, contracts1=contracts1,
            symbol2=symbol2, transaction2=transaction2, contracts2=contracts2,
            limit_type=limit_type, net_price=net_price,
        )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def place_option_spread(
    symbol1: str,
    transaction1: str,
    symbol2: str,
    transaction2: str,
    limit_type: str,
    net_price: float,
    confirm_token: str,
    contracts1: int = 1,
    contracts2: int = 1,
) -> str:
    """Place a real two-leg option spread (dry_run=False). Requires FT_ALLOW_LIVE_ORDERS=true
    in .env AND a confirm_token from preview_option_spread called with these exact same
    arguments — the server rejects the order otherwise, it does not just rely on the
    caller having "meant to" preview first.

    Same arguments as preview_option_spread, plus confirm_token. DAY order only;
    7AM–4PM ET window. Returns JSON with order confirmation. This sends a real
    order to Firstrade.
    """
    gate_err = _require_live_orders_enabled()
    if gate_err:
        return json.dumps({"error": gate_err})
    tok_err = _check_preview_token(
        "spread", confirm_token, symbol1=symbol1, transaction1=transaction1, contracts1=contracts1,
        symbol2=symbol2, transaction2=transaction2, contracts2=contracts2,
        limit_type=limit_type, net_price=net_price,
    )
    if tok_err:
        return json.dumps({"error": tok_err})
    return _spread_order(symbol1, transaction1, contracts1, symbol2, transaction2, contracts2,
                         limit_type, net_price, dry_run=False)


@mcp.tool()
def cancel_order(order_id: str) -> str:
    """Cancel an open order by order_id (e.g. 'G42621-1569').

    Args:
        order_id: The order ID returned by place_stock_order or place_option_order.

    Returns JSON with cancellation result.
    """
    session, data = _get_data()
    acct, err = _select_account(data)
    if err:
        return err
    from firstrade import urls
    resp = session._request("post", url=urls.cancel_order(), data={"order_id": order_id})
    return json.dumps(resp.json(), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
