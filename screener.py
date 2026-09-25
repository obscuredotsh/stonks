#!/usr/bin/env python3
"""Auto-update momentum_picks.txt with the highest-momentum + most liquid + jumpiest names.

Workflow:
  1. Read universe.txt — a list of candidate NSE tickers.
  2. Pull 6 months of daily candles for each via yfinance (one batch download).
  3. Score every symbol on three axes (each z-scored across the universe):
       · momentum   — 1mo + 3mo returns + (-distance-from-52w-high)
       · liquidity  — mean(₹ turnover) over last 20 days  (log-scaled)
       · jumpiness  — ATR/price + biggest 5-day return in last 20 sessions
  4. Drop anything that fails basic sanity (penny stock, illiquid, broken).
  5. Sort by composite score, write top N to momentum_picks.txt.

Example:
    python screener.py                          # top 15 → momentum_picks.txt
    python screener.py --top 25
    python screener.py --w-momentum 0.7 --w-liquidity 0.2 --w-jump 0.1
    python screener.py --side long              # bullish-only ranking
    python screener.py --side short             # bearish ranking (worst momentum first)
    python screener.py --dry-run                # show ranking, don't write
"""
from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import groww_client  # NSE data via Groww API; replaces yfinance
from rich.console import Console
from rich.table import Table

SCRIPT_DIR = Path(__file__).resolve().parent
console = Console()

# Sanity floors — anything below these is dropped before ranking.
MIN_PRICE_INR = 50.0          # avoid pennies
MIN_TURNOVER_CR = 5.0         # ₹ crore/day average — need real liquidity
MIN_BARS = 80                 # need ~4 months of daily history
MAX_DRAWDOWN_FROM_52W_HIGH = 0.40  # skip stocks more than 40% below their 52w high


@dataclass
class Row:
    symbol: str
    price: float
    mom_1m: float           # 1-month return (~22 trading days)
    mom_3m: float           # 3-month return (~66 trading days)
    dist_52w_high: float    # close / 52w_high - 1   (negative = below high)
    turnover_cr: float      # ₹ crore, avg over last 20 sessions
    atr_pct: float          # ATR(14) / close
    best_5d: float          # max 5-day close-to-close return in last 20 sessions
    score: float = 0.0
    rank: int = 0


def _load_universe(path: Path) -> list[str]:
    out: list[str] = []
    for line in path.read_text().splitlines():
        s = line.split("#", 1)[0].strip()
        if s:
            out.append(s.upper())
    return sorted(set(out))


def _atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def _summarize(symbol: str, df: pd.DataFrame) -> Optional[Row]:
    if df is None or len(df) < MIN_BARS:
        return None
    df = df.dropna(how="all").copy()
    if "Close" not in df.columns or df["Close"].isna().all():
        return None

    close = df["Close"]
    price = float(close.iloc[-1])
    if not math.isfinite(price) or price < MIN_PRICE_INR:
        return None

    # Returns
    def ret(n):
        if len(close) < n + 1:
            return 0.0
        p0 = float(close.iloc[-n - 1])
        return (price / p0 - 1.0) if p0 > 0 else 0.0

    mom_1m = ret(22)
    mom_3m = ret(66)

    high_52w = float(df["High"].rolling(252, min_periods=20).max().iloc[-1])
    dist_52 = (price / high_52w - 1.0) if high_52w > 0 else 0.0

    turnover_inr = (close * df["Volume"]).rolling(20).mean().iloc[-1]
    turnover_cr = float(turnover_inr) / 1e7 if pd.notna(turnover_inr) else 0.0  # crore

    df["ATR"] = _atr(df)
    atr_last = float(df["ATR"].iloc[-1]) if pd.notna(df["ATR"].iloc[-1]) else 0.0
    atr_pct = (atr_last / price) if price > 0 else 0.0

    last20_close = close.iloc[-21:]
    if len(last20_close) >= 6:
        rolling5 = last20_close.pct_change(5).abs()
        best_5d = float(rolling5.iloc[-15:].max()) if len(rolling5) >= 15 else float(rolling5.max())
    else:
        best_5d = 0.0

    if dist_52 < -MAX_DRAWDOWN_FROM_52W_HIGH:
        return None
    if turnover_cr < MIN_TURNOVER_CR:
        return None

    return Row(
        symbol=symbol, price=price,
        mom_1m=mom_1m, mom_3m=mom_3m, dist_52w_high=dist_52,
        turnover_cr=turnover_cr, atr_pct=atr_pct, best_5d=best_5d,
    )


def _zscore(xs: list[float]) -> list[float]:
    a = np.array(xs, dtype=float)
    mu = a.mean()
    sd = a.std(ddof=0)
    if sd == 0:
        return [0.0] * len(xs)
    return ((a - mu) / sd).tolist()


def _score_rows(
    rows: list[Row], *,
    w_momentum: float, w_liquidity: float, w_jump: float, side: str,
) -> list[Row]:
    if not rows:
        return rows

    # Build sub-scores. Momentum is a blend; liquidity is log-turnover; jumpiness is ATR% + best-5d.
    mom_blend = [(r.mom_1m + r.mom_3m + (-r.dist_52w_high)) / 3.0 for r in rows]
    liq = [math.log10(max(r.turnover_cr, MIN_TURNOVER_CR)) for r in rows]
    jump = [r.atr_pct + r.best_5d for r in rows]

    mom_z = _zscore(mom_blend)
    liq_z = _zscore(liq)
    jump_z = _zscore(jump)

    for i, r in enumerate(rows):
        composite = (
            w_momentum * mom_z[i]
            + w_liquidity * liq_z[i]
            + w_jump * jump_z[i]
        )
        if side == "short":
            composite = -w_momentum * mom_z[i] + w_liquidity * liq_z[i] + w_jump * jump_z[i]
        r.score = composite

    rows.sort(key=lambda r: -r.score)
    for i, r in enumerate(rows):
        r.rank = i + 1
    return rows


def _fetch_universe(symbols: list[str], period: str) -> dict[str, pd.DataFrame]:
    """Download daily candles for a list of NSE symbols via Groww.

    Groww doesn't have a single batch endpoint like yfinance; we loop one
    symbol at a time. For a 150-name universe that's ~150 sequential API
    calls — slower than yfinance's parallel batch but well under Groww's
    free-tier rate limit (currently ~3 req/sec). At default `period=6mo`
    each name returns ~120 daily rows, so this is bounded both in
    bandwidth and time (a few seconds for the whole universe).
    """
    console.print(f"  [dim]downloading {len(symbols)} symbols ({period} daily) from Groww…[/dim]")
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = groww_client.fetch(sym, period=period, interval="1d")
        if df is None or df.empty:
            continue
        out[sym] = df
    return out


def render_rankings(rows: list[Row], n: int):
    t = Table(title=f"Top {min(n, len(rows))} (of {len(rows)} that passed filters)",
              header_style="bold")
    t.add_column("#", justify="right", style="dim")
    t.add_column("Symbol", style="cyan")
    t.add_column("Price ₹", justify="right")
    t.add_column("1m %", justify="right")
    t.add_column("3m %", justify="right")
    t.add_column("vs 52wH", justify="right")
    t.add_column("Turnover (₹Cr)", justify="right")
    t.add_column("ATR %", justify="right")
    t.add_column("Best 5d %", justify="right")
    t.add_column("Score", justify="right")

    def colorpct(x: float) -> str:
        c = "green" if x > 0 else ("red" if x < 0 else "white")
        return f"[{c}]{x*100:+.1f}%[/{c}]"

    for r in rows[:n]:
        t.add_row(
            str(r.rank), r.symbol, f"{r.price:,.2f}",
            colorpct(r.mom_1m), colorpct(r.mom_3m),
            colorpct(r.dist_52w_high),
            f"{r.turnover_cr:,.1f}",
            f"{r.atr_pct*100:.1f}%",
            colorpct(r.best_5d),
            f"[bold]{r.score:+.2f}[/bold]",
        )
    console.print(t)


def write_watchlist(rows: list[Row], n: int, path: Path, side: str) -> None:
    top = rows[:n]
    lines = [
        f"# Auto-generated by screener.py · {pd.Timestamp.now(tz='Asia/Kolkata'):%Y-%m-%d %H:%M %Z}",
        f"# Side: {side}  ·  picked {len(top)}/{len(rows)} after liquidity/price/drawdown filters",
        "# Composite z-score = w_momentum·momentum + w_liquidity·log(turnover) + w_jump·(ATR% + best-5d)",
        "",
    ]
    for r in top:
        lines.append(
            f"{r.symbol:<14}  # rank {r.rank:>2}  score {r.score:+.2f}  "
            f"1m {r.mom_1m*100:+.1f}%  3m {r.mom_3m*100:+.1f}%  "
            f"₹{r.turnover_cr:.0f}Cr  ATR {r.atr_pct*100:.1f}%"
        )
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe", type=Path, default=SCRIPT_DIR / "universe.txt")
    ap.add_argument("--output", type=Path, default=SCRIPT_DIR / "momentum_picks.txt")
    ap.add_argument("--top", type=int, default=15, help="how many tickers to keep")
    ap.add_argument("--period", default="6mo", help="yfinance lookback for the data fetch")
    ap.add_argument("--w-momentum",  type=float, default=0.55, help="momentum weight (default 0.55)")
    ap.add_argument("--w-liquidity", type=float, default=0.25, help="liquidity weight (default 0.25)")
    ap.add_argument("--w-jump",      type=float, default=0.20, help="jumpiness weight (default 0.20)")
    ap.add_argument("--side", choices=["long", "short"], default="long",
                    help="long: rank by uptrend; short: rank by downtrend (default long)")
    ap.add_argument("--dry-run", action="store_true", help="print rankings, don't rewrite momentum_picks.txt")
    args = ap.parse_args()

    if not args.universe.exists():
        console.print(f"[red]No universe at {args.universe}[/red]")
        sys.exit(2)

    symbols = _load_universe(args.universe)
    console.rule(f"[bold]Screener  •  {len(symbols)} candidates  •  side={args.side}  •  top {args.top}[/bold]")

    data = _fetch_universe(symbols, args.period)
    if not data:
        console.print("[red]no data returned — check connectivity / yfinance throttle[/red]")
        sys.exit(1)

    rows: list[Row] = []
    skipped = 0
    for sym in symbols:
        df = data.get(sym)
        row = _summarize(sym, df) if df is not None else None
        if row is None:
            skipped += 1
            continue
        rows.append(row)

    if not rows:
        console.print("[red]every symbol filtered out — relax MIN_TURNOVER_CR / MIN_PRICE_INR[/red]")
        sys.exit(1)

    rows = _score_rows(
        rows,
        w_momentum=args.w_momentum,
        w_liquidity=args.w_liquidity,
        w_jump=args.w_jump,
        side=args.side,
    )
    console.print(
        f"  [dim]kept {len(rows)} / dropped {skipped} (penny / illiquid / no data / "
        f">{int(MAX_DRAWDOWN_FROM_52W_HIGH*100)}% off 52w high)[/dim]\n"
    )
    render_rankings(rows, args.top)

    if args.dry_run:
        console.print("\n[yellow]--dry-run set; momentum_picks.txt was NOT modified.[/yellow]")
        return

    write_watchlist(rows, args.top, args.output, args.side)
    console.print(f"\n[green]wrote {min(args.top, len(rows))} symbols → {args.output}[/green]")


if __name__ == "__main__":
    main()
