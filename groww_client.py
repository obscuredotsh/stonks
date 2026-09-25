"""Thin Groww-API adapter that mimics yfinance's data contract.

Why this exists
---------------
The original codebase fetched OHLCV via `yfinance.download()`, which returns
a DataFrame with a tz-aware DateTimeIndex and columns Open/High/Low/Close/Volume.
yfinance data on NSE is ~15 min delayed; this adapter swaps that for the
official Groww Trade API which gives real-time prices for the authenticated
user's NSE-enabled account.

Public surface
--------------
    fetch(ticker, period, interval) -> DataFrame | None
        Drop-in replacement for the yfinance call. Accepts a yfinance-style
        ticker (`"RELIANCE.NS"` or plain `"RELIANCE"`) and returns the same
        DataFrame shape so analyze.py / screener.py don't need to change.

    get_ltp_batch(symbols) -> dict[str, float]
        Real-time LTP for a list of symbols. Used by app.py to refresh the
        "Now" column on every scan without re-running the full historical
        download.

Credentials are loaded from .env next to this file (loaded once at import).
Never log them; never echo them to stdout.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
from dotenv import load_dotenv
from growwapi import GrowwAPI

# ── Credentials ─────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent / ".env")

_API_KEY = os.environ.get("GROWW_API_KEY")
_API_SECRET = os.environ.get("GROWW_API_SECRET")

# ── Singleton client (mint token once per process) ──────────────────
_CLIENT: Optional[GrowwAPI] = None
_CLIENT_LOCK = threading.Lock()

# Last error seen while fetching live quotes (LTP). Surfaced to the UI so a
# missing "Live Data" subscription is explained instead of silently showing
# a fake -100% P&L. None means "no error since last successful call".
_LAST_QUOTE_ERROR: Optional[str] = None


def last_quote_error() -> Optional[str]:
    """Return the most recent live-quote error string (or None)."""
    return _LAST_QUOTE_ERROR


def _client() -> GrowwAPI:
    """Return a singleton GrowwAPI instance. Mints the access token on first
    call; subsequent calls reuse it for the lifetime of the process."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        if not _API_KEY or not _API_SECRET:
            raise RuntimeError(
                "GROWW_API_KEY / GROWW_API_SECRET missing — set them in .env"
            )
        token = GrowwAPI.get_access_token(api_key=_API_KEY, secret=_API_SECRET)
        # SDK sometimes wraps the token in a dict, sometimes returns it raw.
        if isinstance(token, dict):
            token = token.get("access_token") or token.get("token") or token
        _CLIENT = GrowwAPI(token)
        return _CLIENT


# ── Period / interval translation ───────────────────────────────────
# yfinance period strings → number of calendar days to request from Groww.
_PERIOD_DAYS = {
    "1mo": 30, "3mo": 90, "6mo": 180,
    "1y": 365, "2y": 730, "5y": 1825,
}

# yfinance interval strings → Groww candle_interval (in minutes).
# Daily is 1440 minutes. Weekly isn't natively supported, so we fetch daily
# and resample at the end of fetch().
_INTERVAL_MIN = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60,
    "1d": 1440, "1wk": 1440,
}


def _strip_suffix(ticker: str) -> str:
    """yfinance: `RELIANCE.NS`. Groww: `RELIANCE`."""
    if ticker.endswith(".NS"):
        return ticker[:-3]
    if ticker.endswith(".BO") or ticker.endswith(".BSE"):
        return ticker.split(".")[0]
    return ticker


# ── Main fetch (drop-in for yf.download) ────────────────────────────
def fetch(
    ticker: str,
    period: str = "1y",
    interval: str = "1d",
) -> Optional[pd.DataFrame]:
    """Return OHLCV DataFrame matching yfinance's shape.

    Index: tz-aware DatetimeIndex in Asia/Kolkata.
    Columns: Open, High, Low, Close, Volume (all float).
    Returns None on any data or auth failure (analyze.py logs the symbol).
    """
    sym = _strip_suffix(ticker)
    days = _PERIOD_DAYS.get(period, 365)
    iv = _INTERVAL_MIN.get(interval, 1440)
    weekly = interval == "1wk"

    # Generous fetch window — Groww's start_time is inclusive but trading
    # holidays + weekends eat into the actual bar count. +60 cushions us.
    end = dt.datetime.now()
    start = end - dt.timedelta(days=days + 60)
    fmt = "%Y-%m-%d %H:%M:%S"

    try:
        resp = _client().get_historical_candle_data(
            trading_symbol=sym,
            exchange="NSE",
            segment="CASH",
            start_time=start.strftime(fmt),
            end_time=end.strftime(fmt),
            interval_in_minutes=iv,
        )
    except Exception:
        return None

    rows = resp.get("candles") if isinstance(resp, dict) else None
    if not rows:
        return None

    df = pd.DataFrame(
        rows,
        columns=["ts", "Open", "High", "Low", "Close", "Volume"],
    )
    # Groww timestamps are Unix epoch seconds (UTC).
    df["ts"] = (
        pd.to_datetime(df["ts"], unit="s", utc=True)
        .dt.tz_convert("Asia/Kolkata")
    )
    df = df.set_index("ts").sort_index()
    for col in ("Open", "High", "Low", "Close", "Volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(how="all")

    if weekly:
        df = (
            df.resample("W-FRI")
            .agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            })
            .dropna()
        )

    return df


# ── Portfolio (holdings + live LTP join + P&L) ──────────────────────
def get_portfolio() -> list[dict]:
    """Return the authenticated user's NSE equity holdings, joined with live
    LTP and unrealized-P&L math. Each row:

        {
          symbol, quantity, avg_price, ltp,
          invested, current_value, pnl_abs, pnl_pct,
          isin
        }

    `ltp` / `current_value` / `pnl_*` are None for any holding whose live
    price couldn't be fetched (rare, but defensive).
    """
    raw = _client().get_holdings_for_user()
    rows = raw.get("holdings", []) if isinstance(raw, dict) else []
    if not rows:
        return []

    symbols = [r.get("trading_symbol") for r in rows if r.get("trading_symbol")]
    ltps = get_ltp_batch(symbols)

    out: list[dict] = []
    for r in rows:
        sym = r.get("trading_symbol")
        if not sym:
            continue
        try:
            qty = float(r.get("quantity") or 0)
            avg = float(r.get("average_price") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        ltp = ltps.get(sym)
        invested = qty * avg
        cur_value = qty * ltp if ltp is not None else None
        pnl_abs = (cur_value - invested) if cur_value is not None else None
        pnl_pct = ((ltp - avg) / avg * 100) if (ltp is not None and avg > 0) else None
        out.append({
            "symbol": sym,
            "quantity": qty,
            "avg_price": round(avg, 2),
            "ltp": round(ltp, 2) if ltp is not None else None,
            "invested": round(invested, 2),
            "current_value": round(cur_value, 2) if cur_value is not None else None,
            "pnl_abs": round(pnl_abs, 2) if pnl_abs is not None else None,
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "isin": r.get("isin"),
        })
    # Biggest current position first.
    out.sort(key=lambda x: -(x["current_value"] or 0))
    return out


# ── Live LTP batch (bonus capability yfinance doesn't have) ─────────
def get_ltp_batch(symbols: Iterable[str]) -> dict[str, float]:
    """Real-time last-traded-price for the given NSE equity symbols.

    Symbols may be plain (`"RELIANCE"`) or yfinance-style (`"RELIANCE.NS"`).
    Returns a dict {plain_symbol: ltp}. Missing symbols are simply absent.
    """
    global _LAST_QUOTE_ERROR
    plain = [_strip_suffix(s) for s in symbols if s]
    if not plain:
        return {}
    pairs = tuple(f"NSE_{s}" for s in plain)
    try:
        resp = _client().get_ltp(
            exchange_trading_symbols=pairs,
            segment="CASH",
        )
        _LAST_QUOTE_ERROR = None
    except Exception as e:  # noqa: BLE001
        _LAST_QUOTE_ERROR = f"{type(e).__name__}: {e}"
        return {}
    if not isinstance(resp, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in resp.items():
        sym = k[len("NSE_"):] if k.startswith("NSE_") else k
        try:
            out[sym] = float(v)
        except (TypeError, ValueError):
            pass
    return out
