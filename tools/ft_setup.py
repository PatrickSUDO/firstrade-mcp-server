"""
Firstrade auth setup. Credentials come from firstrade-server/.env (FT_* keys).

Manual (MFA code typed by hand):
  python3 ft_setup.py step1          -- triggers MFA/OTP, saves a t_token
  python3 ft_setup.py step2 <code>   -- completes auth, saves session cookies

Headless (requires FT_TOTP_SECRET = authenticator-app seed in .env):
  python3 ft_setup.py step2          -- code auto-generated from FT_TOTP_SECRET
  python3 ft_setup.py auto           -- step1 + step2 in one shot, no human

The `auto` path is what launchd / the daily briefing should call to self-heal a
session before it 401s.
"""
import sys, os, json, requests
from firstrade.account import FTSession, FTAccountData
from firstrade import urls

STATE_FILE = "/tmp/ft_auth_state.json"
PROFILE    = os.path.expanduser("~/.local/share/firstrade-session")


def _load_env():
    """Load FT_* creds from the server-dir .env (one level up). Keeps secrets
    out of source — mirrors server.py's loader so both read the same file."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
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

USERNAME = os.environ.get("FT_USERNAME", "")
PASSWORD = os.environ.get("FT_PASSWORD", "")
EMAIL    = os.environ.get("FT_EMAIL", "")
TOTP_SECRET = os.environ.get("FT_TOTP_SECRET", "")  # set this → fully headless refresh

if not (USERNAME and PASSWORD and EMAIL):
    sys.exit("Missing FT_USERNAME / FT_PASSWORD / FT_EMAIL in firstrade-server/.env")


def _totp_code() -> str | None:
    """Generate the current 6-digit TOTP code from FT_TOTP_SECRET, if configured.
    Returns None when no secret is set (fall back to manual code entry)."""
    if not TOTP_SECRET:
        return None
    try:
        import pyotp
    except ImportError:
        sys.exit("FT_TOTP_SECRET is set but pyotp is not installed — run: uv add pyotp")
    return pyotp.TOTP(TOTP_SECRET.replace(" ", "")).now()


def step1():
    s = FTSession(username=USERNAME, password=PASSWORD, email=EMAIL,
                  profile_path=PROFILE, save_session=True)

    s.session.headers.update(urls.session_headers())
    s.session.headers["access-token"] = urls.access_token()
    s._request("get", url="https://api3x.firstrade.com/", timeout=10)

    resp = s._request("post", url=urls.login(),
                      data={"username": USERNAME, "password": PASSWORD})
    login_json = resp.json()
    t_token = login_json.get("t_token")
    print("Login response:", login_json)

    otp_options = login_json.get("otp")
    if otp_options:
        # Find matching email option
        recipient_id = None
        for item in otp_options:
            if item.get("channel") == "email":
                recipient_id = item["recipientId"]
                print(f"Sending OTP to email: {item.get('recipientMask')}")
                break
        if recipient_id:
            otp_resp = s._request("post", url=urls.request_code(),
                                  data={"recipientId": recipient_id, "t_token": t_token})
            otp_json = otp_resp.json()
            print("OTP request result:", otp_json)
            verification_sid = otp_json.get("verificationSid", "")
        else:
            verification_sid = login_json.get("verificationSid", "")
    else:
        verification_sid = login_json.get("verificationSid", "")

    # Save state
    state = {
        "headers": dict(s.session.headers),
        "cookies": requests.utils.dict_from_cookiejar(s.session.cookies),
        "t_token": t_token,
        "verification_sid": verification_sid,
        "mfa": login_json.get("mfa", False),
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

    print("✅ OTP sent. Run step2 with the code:")
    print(f"   uv run python3 tools/ft_setup.py step2 <CODE>")


def step2(code=None):
    if code is None:
        code = _totp_code()
        if code is None:
            sys.exit("step2 needs a code: pass it as an argument, or set FT_TOTP_SECRET in .env")
    with open(STATE_FILE) as f:
        state = json.load(f)

    s = FTSession(username=USERNAME, password=PASSWORD, email=EMAIL,
                  profile_path=PROFILE, save_session=True)
    s.session.headers.update(state["headers"])
    for k, v in state["cookies"].items():
        s.session.cookies.set(k, v)

    s.t_token = state["t_token"]
    s.login_json = {"mfa": state["mfa"]}

    if state["mfa"]:
        data = {"mfaCode": code, "remember_for": "30", "t_token": state["t_token"]}
    else:
        data = {"otpCode": code, "verificationSid": state["verification_sid"],
                "remember_for": "30", "t_token": state["t_token"]}

    resp = s._request("post", url=urls.verify_pin(), data=data)
    result = resp.json()
    print("Verify result:", result)

    if result.get("error"):
        print("❌ Error:", result["error"])
        sys.exit(1)

    s.session.headers["ftat"] = result.get("ftat", "")
    s.session.headers["sid"]  = result.get("sid", "")
    s._save_cookies()

    os.makedirs(os.path.dirname(PROFILE) if os.path.dirname(PROFILE) else ".", exist_ok=True)

    d = FTAccountData(s)
    print("✅ Authentication complete!")
    print("Accounts:", d.account_numbers)
    print("Balances:", d.account_balances)
    print(f"Session saved to: {PROFILE}")


def auto():
    """Fully headless refresh: step1 → TOTP code → step2, no human in the loop.
    Requires FT_TOTP_SECRET in .env. Usable by launchd / the daily briefing."""
    if not TOTP_SECRET:
        sys.exit("`auto` needs FT_TOTP_SECRET in .env (the authenticator-app seed). "
                 "Use step1 + step2 <code> for manual refresh until then.")
    step1()
    code = _totp_code()
    print(f"Auto-generated TOTP code (using FT_TOTP_SECRET)")
    step2(code)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "step1":
        step1()
    elif sys.argv[1] == "step2":
        # code optional: uses FT_TOTP_SECRET when omitted
        step2(sys.argv[2] if len(sys.argv) == 3 else None)
    elif sys.argv[1] == "auto":
        auto()
    else:
        print(__doc__)
        sys.exit(1)
