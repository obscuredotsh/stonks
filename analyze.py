#!/usr/bin/env python3
"""Swing signal scanner for NSE-listed stocks (daily candles).

Pulls daily OHLCV from Yahoo Finance and emits a composite buy/sell
signal from seven independent technical voters (EMA20/50, EMA200,
RSI, MACD, Bollinger, Volume + 20d breakout, Supertrend) with an
ADX(14) trend-strength gate. Educational use only.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

import groww_client  # real-time NSE data via Groww API; replaces yfinance
from rich.console import Console
from rich.table import Table
from rich.text import Text

IST = "Asia/Kolkata"
SCRIPT_DIR = Path(__file__).resolve().parent
console = Console()


# ──────────────────────────────────────────────────────────────────────────
# Indicators (pure pandas, no native deps)
# ──────────────────────────────────────────────────────────────────────────
def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def bollinger(close: pd.Series, length: int = 20, k: float = 2.0):
    mid = close.rolling(length).mean()
    sd = close.rolling(length).std(ddof=0)
    return mid - k * sd, mid, mid + k * sd


def adx(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    """Wilder's ADX (1978). Returns DataFrame with +DI, -DI, ADX columns.

    ADX measures *trend strength* independent of direction.
    - ADX < 20: choppy / sideways market (trend systems shouldn't trade)
    - ADX 20-25: emerging trend
    - ADX 25-40: solid trend
    - ADX > 40: strong / mature trend (may be late)
    """
    import numpy as np
    h, l, c = df["High"], df["Low"], df["Close"]
    up = h.diff()
    down = -l.diff()
    plus_dm = ((up > down) & (up > 0)).astype(float) * up.fillna(0.0)
    minus_dm = ((down > up) & (down > 0)).astype(float) * down.fillna(0.0)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / length, adjust=False).mean().replace(0, np.nan)
    plus_di = (100 * plus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_).astype(float)
    minus_di = (100 * minus_dm.ewm(alpha=1 / length, adjust=False).mean() / atr_).astype(float)
    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / di_sum).astype(float)
    adx_ = dx.ewm(alpha=1 / length, adjust=False).mean()
    return pd.DataFrame({"+DI": plus_di, "-DI": minus_di, "ADX": adx_})


def supertrend(df: pd.DataFrame, length: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """Supertrend indicator (Olivier Seban). Returns DataFrame with ST_dir and ST_line.

    ST_dir is +1 (uptrend) or -1 (downtrend). Trend flips when price closes
    on the other side of the trailing band. Hugely popular in Indian retail
    trading; works as both a directional filter and a trailing stop.
    """
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr_ = tr.ewm(alpha=1 / length, adjust=False).mean()

    hl2 = (h + l) / 2
    upper_band = hl2 + multiplier * atr_
    lower_band = hl2 - multiplier * atr_

    n = len(df)
    direction = [0] * n
    line = [0.0] * n
    for i in range(n):
        if i == 0 or pd.isna(atr_.iloc[i]):
            direction[i] = 1
            line[i] = lower_band.iloc[i] if pd.notna(lower_band.iloc[i]) else float(c.iloc[i])
            continue
        prev_dir = direction[i - 1]
        prev_line = line[i - 1]
        ub, lb = float(upper_band.iloc[i]), float(lower_band.iloc[i])
        close_i = float(c.iloc[i])
        if prev_dir == 1:
            if close_i < prev_line:
                direction[i] = -1
                line[i] = ub
            else:
                direction[i] = 1
                line[i] = max(lb, prev_line)
        else:
            if close_i > prev_line:
                direction[i] = 1
                line[i] = lb
            else:
                direction[i] = -1
                line[i] = min(ub, prev_line)
    return pd.DataFrame(
        {"ST_dir": direction, "ST_line": line},
        index=df.index,
    )


# ──────────────────────────────────────────────────────────────────────────
# Signal engine
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class Vote:
    name: str
    score: int  # -1, 0, +1
    detail: str


@dataclass
class Analysis:
    symbol: str
    last_price: float
    last_time: pd.Timestamp
    votes: list[Vote]
    total: int
    label: str
    label_color: str
    entry: Optional[float]
    stop: Optional[float]
    target: Optional[float]
    note: str


def classify(total: int, voters: int) -> tuple[str, str]:
    if total >= voters - 1:
        return "STRONG BUY", "bold green"
    if total >= 2:
        return "BUY", "green"
    if total <= -(voters - 1):
        return "STRONG SELL", "bold red"
    if total <= -2:
        return "SELL", "red"
    return "HOLD / WAIT", "yellow"


def fetch(ticker: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    """Fetch OHLCV via Groww. Returns the same DataFrame shape as the prior
    yfinance.download() call so the rest of analyze.py stays untouched."""
    try:
        df = groww_client.fetch(ticker, period=period, interval=interval)
    except Exception as e:
        console.print(f"[red]download failed[/red] {ticker}: {e}")
        return None
    if df is None or df.empty:
        return None
    # groww_client already returns IST-aware timestamps, but be defensive.
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(IST)
    return df


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Attach the swing voters' raw inputs as columns. Idempotent."""
    if "EMA50" in df.columns:
        return df
    df["EMA20"] = ema(df["Close"], 20)
    df["EMA50"] = ema(df["Close"], 50)
    df["EMA200"] = ema(df["Close"], 200)
    df["RSI"] = rsi(df["Close"])
    df["ATR"] = atr(df)
    df["BBL"], df["BBM"], df["BBU"] = bollinger(df["Close"], length=20, k=2.0)
    df["VolMA"] = df["Volume"].rolling(20).mean()
    ema12 = ema(df["Close"], 12)
    ema26 = ema(df["Close"], 26)
    df["MACD"] = ema12 - ema26
    df["MACDsig"] = ema(df["MACD"], 9)
    df["MACDhist"] = df["MACD"] - df["MACDsig"]
    adx_df = adx(df, length=14)
    df["ADX"] = adx_df["ADX"]
    df["+DI"] = adx_df["+DI"]
    df["-DI"] = adx_df["-DI"]
    st_df = supertrend(df, length=10, multiplier=3.0)
    df["ST_dir"] = st_df["ST_dir"]
    df["ST_line"] = st_df["ST_line"]
    return df


SWING_ADX_FLOOR = 20.0  # below this, mark signals as non-actionable (choppy market)


def analyze_df(
    symbol: str,
    df: pd.DataFrame,
    *,
    quiet: bool = False,
    adx_floor: float = SWING_ADX_FLOOR,
) -> Optional[Analysis]:
    """Swing-mode analyzer for daily / weekly candles.

    Seven voters: EMA20/50 cross, price vs EMA200, RSI(14), MACD(12,26,9),
    Bollinger(20,2) — re-interpreted as momentum-continuation rather than
    mean-reversion — volume-confirmed 20-day breakout / breakdown, and
    Supertrend(10, 3.0).

    Robustness layer: trades are flagged actionable (entry/stop/target set)
    only when the ADX(14) is above `adx_floor` (default 20) — i.e. when the
    market is actually trending. In choppy regimes the label still shows
    but no entry levels are emitted, so neither the live UI nor the
    backtester will fire on the signal.

    Stops: 2× ATR(14). Targets: 1:3 R:R. Hold across days; force-flat is
    only enforced by the backtester (max_hold_bars).
    """
    if df is None or len(df) < 60:
        if not quiet:
            console.print(f"[red]not enough data[/red] for {symbol} (need 60+ bars, got {0 if df is None else len(df)})")
        return None

    df = compute_indicators(df)

    last = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(last["Close"])
    votes: list[Vote] = []

    # 1. EMA 20/50 cross — short/medium trend
    e20, e50 = float(last["EMA20"]), float(last["EMA50"])
    p20, p50 = float(prev["EMA20"]), float(prev["EMA50"])
    if e20 > e50 and p20 <= p50:
        votes.append(Vote("EMA20/50", +1, "golden cross"))
    elif e20 < e50 and p20 >= p50:
        votes.append(Vote("EMA20/50", -1, "death cross"))
    elif e20 > e50:
        votes.append(Vote("EMA20/50", +1, "20 above 50"))
    elif e20 < e50:
        votes.append(Vote("EMA20/50", -1, "20 below 50"))
    else:
        votes.append(Vote("EMA20/50", 0, "flat"))

    # 2. Price vs 200-EMA — long-term stage filter
    if pd.notna(last["EMA200"]):
        e200 = float(last["EMA200"])
        gap = (price / e200 - 1.0) * 100
        if gap > 2.0:
            votes.append(Vote("EMA200", +1, f"above 200d {gap:+.1f}%"))
        elif gap < -2.0:
            votes.append(Vote("EMA200", -1, f"below 200d {gap:+.1f}%"))
        else:
            votes.append(Vote("EMA200", 0, f"at 200d {gap:+.1f}%"))
    else:
        votes.append(Vote("EMA200", 0, "<200 bars"))

    # 3. RSI(14)
    r = float(last["RSI"]) if pd.notna(last["RSI"]) else 50.0
    if r < 30:
        votes.append(Vote("RSI(14)", +1, f"oversold {r:.0f}"))
    elif r > 70:
        votes.append(Vote("RSI(14)", -1, f"overbought {r:.0f}"))
    elif r > 55:
        votes.append(Vote("RSI(14)", +1, f"bullish {r:.0f}"))
    elif r < 45:
        votes.append(Vote("RSI(14)", -1, f"bearish {r:.0f}"))
    else:
        votes.append(Vote("RSI(14)", 0, f"neutral {r:.0f}"))

    # 4. MACD histogram — momentum
    h = float(last["MACDhist"])
    ph = float(prev["MACDhist"]) if pd.notna(prev["MACDhist"]) else h
    if h > 0 and ph <= 0:
        votes.append(Vote("MACD", +1, "bullish cross"))
    elif h < 0 and ph >= 0:
        votes.append(Vote("MACD", -1, "bearish cross"))
    elif h > 0 and h > ph:
        votes.append(Vote("MACD", +1, f"rising +ve ({h:+.2f})"))
    elif h < 0 and h < ph:
        votes.append(Vote("MACD", -1, f"falling -ve ({h:+.2f})"))
    else:
        votes.append(Vote("MACD", 0, f"flat ({h:+.2f})"))

    # 5. Bollinger 20/2 — momentum-continuation interpretation for swing
    bbl, bbm, bbu = float(last["BBL"]), float(last["BBM"]), float(last["BBU"])
    if price > bbu:
        votes.append(Vote("BB(20,2)", +1, "breakout above upper"))
    elif price < bbl:
        votes.append(Vote("BB(20,2)", -1, "breakdown below lower"))
    else:
        ref_idx = -5 if len(df) >= 6 else -2
        prev_mid = float(df["BBM"].iloc[ref_idx]) if pd.notna(df["BBM"].iloc[ref_idx]) else bbm
        slope = bbm - prev_mid
        if slope > 0 and price > bbm:
            votes.append(Vote("BB(20,2)", +1, "rising mid, price above"))
        elif slope < 0 and price < bbm:
            votes.append(Vote("BB(20,2)", -1, "falling mid, price below"))
        else:
            votes.append(Vote("BB(20,2)", 0, "inside bands"))

    # 6. Volume-confirmed 20-day breakout / breakdown
    if len(df) >= 21:
        recent_high = float(df["Close"].iloc[-21:-1].max())
        recent_low = float(df["Close"].iloc[-21:-1].min())
        vol = float(last["Volume"])
        vol_avg = float(last["VolMA"]) if pd.notna(last["VolMA"]) else 0.0
        ratio = (vol / vol_avg) if vol_avg > 0 else 1.0
        if price > recent_high and ratio > 1.3:
            votes.append(Vote("Vol+Break", +1, f"20d high on {ratio:.1f}x vol"))
        elif price < recent_low and ratio > 1.3:
            votes.append(Vote("Vol+Break", -1, f"20d low on {ratio:.1f}x vol"))
        elif ratio > 1.5:
            direction = +1 if last["Close"] >= last["Open"] else -1
            votes.append(Vote("Vol+Break", direction,
                              f"{ratio:.1f}x avg ({'up' if direction > 0 else 'down'} bar)"))
        else:
            votes.append(Vote("Vol+Break", 0, f"{ratio:.1f}x avg"))
    else:
        votes.append(Vote("Vol+Break", 0, "<20 bars"))

    # 7. Supertrend (10, 3.0) — popular ATR-based trend / trailing-stop indicator
    if "ST_dir" in df.columns and pd.notna(last["ST_dir"]):
        st_dir = int(last["ST_dir"])
        st_line = float(last["ST_line"])
        prev_dir = int(prev["ST_dir"]) if pd.notna(prev["ST_dir"]) else st_dir
        if st_dir == 1 and prev_dir == -1:
            votes.append(Vote("SuperTrend", +1, f"flip → up @ {st_line:.2f}"))
        elif st_dir == -1 and prev_dir == 1:
            votes.append(Vote("SuperTrend", -1, f"flip → down @ {st_line:.2f}"))
        elif st_dir == 1:
            votes.append(Vote("SuperTrend", +1, f"up · trail @ {st_line:.2f}"))
        else:
            votes.append(Vote("SuperTrend", -1, f"down · trail @ {st_line:.2f}"))
    else:
        votes.append(Vote("SuperTrend", 0, "n/a"))

    total = sum(v.score for v in votes)
    label, color = classify(total, voters=len(votes))

    # ADX gate — trend systems should NOT trade in choppy markets.
    # Below adx_floor the label still shows but no entry/stop/target are emitted.
    adx_val = float(last["ADX"]) if "ADX" in df.columns and pd.notna(last["ADX"]) else 0.0
    trending = adx_val >= adx_floor

    a_val = float(last["ATR"]) if pd.notna(last["ATR"]) else 0.0
    entry = stop = target = None
    if not trending:
        note = f"ADX {adx_val:.0f} < {adx_floor:.0f} — choppy regime, skip"
    elif total >= 2 and a_val > 0:
        entry = price
        stop = round(price - 2.0 * a_val, 2)
        target = round(price + 3.0 * (price - stop), 2)
        note = f"long swing  ·  ADX {adx_val:.0f}  ·  2×ATR stop  ·  1:3 R:R"
    elif total <= -2 and a_val > 0:
        entry = price
        stop = round(price + 2.0 * a_val, 2)
        target = round(price - 3.0 * (stop - price), 2)
        note = f"short swing  ·  ADX {adx_val:.0f}  ·  2×ATR stop  ·  1:3 R:R"
    else:
        note = f"ADX {adx_val:.0f} · wait for clearer alignment"

    return Analysis(
        symbol=symbol.upper(),
        last_price=price,
        last_time=last.name,
        votes=votes,
        total=total,
        label=label,
        label_color=color,
        entry=round(entry, 2) if entry is not None else None,
        stop=stop,
        target=target,
        note=note,
    )


def analyze(
    symbol: str,
    period: str = "1y",
    interval: str = "1d",
) -> Optional[Analysis]:
    """Fetch + analyse. Daily candles, 1y lookback by default."""
    ticker = symbol if "." in symbol else f"{symbol}.NS"
    df = fetch(ticker, period, interval)
    return analyze_df(symbol, df)


# ──────────────────────────────────────────────────────────────────────────
# Rendering
# ──────────────────────────────────────────────────────────────────────────
def render(a: Analysis):
    title = Text.assemble(
        (f"{a.symbol}  ", "bold cyan"),
        (f"₹{a.last_price:.2f}  ", "bold"),
        (f"@ {a.last_time:%d-%b %H:%M}  ", "dim"),
        ("→  ", "dim"),
        (a.label, a.label_color),
        (f"  (score {a.total:+d})", "dim"),
    )
    table = Table(title=title, title_justify="left", show_lines=False, header_style="bold")
    table.add_column("Indicator", style="cyan", no_wrap=True)
    table.add_column("Vote", justify="center")
    table.add_column("Detail", style="white")
    glyphs = {1: "[green]+1[/green]", -1: "[red]-1[/red]", 0: "[yellow] 0[/yellow]"}
    for v in a.votes:
        table.add_row(v.name, glyphs[v.score], v.detail)
    console.print(table)
    if a.entry is not None:
        console.print(
            f"  entry [bold]{a.entry}[/bold]  "
            f"stop [red]{a.stop}[/red]  "
            f"target [green]{a.target}[/green]  "
            f"[dim]{a.note}[/dim]"
        )
    else:
        console.print(f"  [dim]{a.note}[/dim]")
    console.print()


def load_watchlist(path: Path) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text().splitlines():
        s = line.split("#", 1)[0].strip()
        if s:
            out.append(s)
    return out


def main():
    p = argparse.ArgumentParser(description="NSE swing signal scanner (daily candles)")
    p.add_argument("symbols", nargs="*", help="NSE tickers, e.g. RELIANCE TCS INFY")
    p.add_argument("-w", "--watchlist", type=Path, default=SCRIPT_DIR / "momentum_picks.txt")
    p.add_argument(
        "-i", "--interval", default="1d",
        choices=["1d", "1wk"],
        help="candle interval (default: 1d)",
    )
    p.add_argument("--period", default="1y",
                   help="yfinance lookback period (default: 1y)")
    p.add_argument("--only", choices=["buy", "sell", "all"], default="all")
    args = p.parse_args()

    symbols = args.symbols or load_watchlist(args.watchlist)
    if not symbols:
        console.print("[red]No symbols. Pass them on the CLI or populate momentum_picks.txt.[/red]")
        sys.exit(2)

    console.rule(
        f"[bold]NSE swing scan  •  {datetime.now():%a %d-%b-%Y %H:%M}  "
        f"•  {args.interval} candles[/bold]"
    )

    results: list[Analysis] = []
    for sym in symbols:
        a = analyze(sym, period=args.period, interval=args.interval)
        if a:
            results.append(a)

    results.sort(key=lambda x: -x.total)
    for a in results:
        if args.only == "buy" and a.total < 2:
            continue
        if args.only == "sell" and a.total > -2:
            continue
        render(a)

    summary = Table(title="Summary (sorted by score)", header_style="bold")
    summary.add_column("Symbol", style="cyan")
    summary.add_column("Price", justify="right")
    summary.add_column("Score", justify="right")
    summary.add_column("Signal")
    summary.add_column("Entry / Stop / Target", style="dim")
    for a in results:
        levels = (
            f"{a.entry} / {a.stop} / {a.target}"
            if a.entry is not None else "-"
        )
        summary.add_row(
            a.symbol,
            f"{a.last_price:.2f}",
            f"{a.total:+d}",
            f"[{a.label_color}]{a.label}[/{a.label_color}]",
            levels,
        )
    console.print(summary)
    console.print(
        "\n[dim]Technical scanner — not financial advice. "
        "Confirm every signal with your own analysis.[/dim]"
    )


if __name__ == "__main__":
    main()
