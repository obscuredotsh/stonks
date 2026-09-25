"""One-shot Groww auth + data probe.

Run:  .venv/bin/python probe_groww.py

Verifies:
  1. Auth via API key + TOTP secret produces a usable access token.
  2. The session can read account info (profile).
  3. We can fetch live LTP for a sample NSE equity.
  4. We can fetch ~250 daily candles (enough for the swing strategy's
     200-day EMA + 14-day ADX warmup).

Outputs a JSON-ish dump for human review. Does NOT place any orders.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from growwapi import GrowwAPI

load_dotenv(Path(__file__).parent / ".env")

API_KEY = os.environ.get("GROWW_API_KEY")
API_SECRET = os.environ.get("GROWW_API_SECRET")
if not API_KEY or not API_SECRET:
    sys.exit("missing GROWW_API_KEY / GROWW_API_SECRET in .env")


def step(n: int, label: str) -> None:
    print(f"\n─── {n}. {label} ".ljust(72, "─"))


# ── 1. Mint an access token ─────────────────────────────────────────
step(1, "Mint access token (api_key + secret → TOTP → access_token)")
try:
    auth = GrowwAPI.get_access_token(api_key=API_KEY, secret=API_SECRET)
    # auth is a dict; print the shape but redact the actual token
    token = auth.get("access_token") if isinstance(auth, dict) else auth
    if isinstance(auth, dict):
        masked = {k: ("…" + str(v)[-8:] if k in {"access_token"} else v)
                  for k, v in auth.items()}
        print(f"  OK  auth dict keys: {list(auth.keys())}")
        print(f"      {masked}")
    else:
        print(f"  OK  raw token (last 12 chars): …{str(auth)[-12:]}")
        token = auth
except Exception as e:
    sys.exit(f"  FAIL  {type(e).__name__}: {e}")

ga = GrowwAPI(token)
print("  OK  GrowwAPI client instantiated")


# ── 2. Profile (low-stakes auth sanity check) ────────────────────────
step(2, "Fetch user profile (verify auth)")
try:
    profile = ga.get_user_profile()
    print(f"  OK  profile keys: {list(profile.keys()) if isinstance(profile, dict) else type(profile).__name__}")
    if isinstance(profile, dict):
        for k in ("user_id", "name", "email", "client_id"):
            if k in profile:
                v = profile[k]
                if k == "email":
                    v = v[:2] + "…" + v[v.find("@"):] if "@" in v else v
                print(f"      {k}: {v}")
except Exception as e:
    print(f"  FAIL  {type(e).__name__}: {e}")


# ── 3. Live LTP ──────────────────────────────────────────────────────
step(3, "Fetch live LTP for RELIANCE, TCS, BHEL (NSE cash segment)")
try:
    ltp = ga.get_ltp(
        exchange_trading_symbols=("NSE_RELIANCE", "NSE_TCS", "NSE_BHEL"),
        segment="CASH",
    )
    print(f"  OK  response type: {type(ltp).__name__}")
    if isinstance(ltp, dict):
        for k, v in list(ltp.items())[:5]:
            print(f"      {k}: {v}")
except Exception as e:
    print(f"  FAIL  {type(e).__name__}: {e}")


# ── 4. Historical daily candles ──────────────────────────────────────
step(4, "Fetch 1y of daily candles for RELIANCE")
end = dt.datetime.now()
start = end - dt.timedelta(days=370)  # generous: ~250 trading days
fmt = "%Y-%m-%d %H:%M:%S"
try:
    candles = ga.get_historical_candle_data(
        trading_symbol="RELIANCE",
        exchange="NSE",
        segment="CASH",
        start_time=start.strftime(fmt),
        end_time=end.strftime(fmt),
        interval_in_minutes=1440,  # daily
    )
    if isinstance(candles, dict):
        keys = list(candles.keys())
        print(f"  OK  response keys: {keys[:8]}{' ...' if len(keys) > 8 else ''}")
        # Try common shapes Groww uses
        rows = candles.get("candles") or candles.get("data") or candles.get("payload")
        if isinstance(rows, list):
            print(f"      candle count: {len(rows)}")
            if rows:
                print(f"      first row: {rows[0]}")
                print(f"      last  row: {rows[-1]}")
        else:
            # Maybe the dict IS the row collection
            print(f"      raw response (first 400 chars): {str(candles)[:400]}")
    elif isinstance(candles, list):
        print(f"  OK  got list, len {len(candles)}")
        if candles:
            print(f"      first: {candles[0]}")
            print(f"      last:  {candles[-1]}")
    else:
        print(f"  ??  response type {type(candles).__name__}: {candles}")
except Exception as e:
    print(f"  FAIL  {type(e).__name__}: {e}")

print("\n─── probe complete ─────────────────────────────────────────────")
