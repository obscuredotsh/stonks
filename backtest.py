#!/usr/bin/env python3
"""Walk-forward backtester for the swing scanner (daily candles, 7 voters).

For each historical bar in a symbol's daily history this script asks the
exact same question the live scanner asks ("if I called analyze() right now,
what would I do?"), opens a virtual position when the score is actionable
AND the ADX(14) gate clears, and exits when the stop or target is hit — or
when the position has been open longer than --max-hold-bars (~ 2 trading
months).

No look-ahead: every decision uses df.iloc[:t+1] only.

Results are reported in **R-multiples** (where 1R = the per-trade risk in
rupees, i.e. |entry − stop|), so position size doesn't muddy the picture.
A profitable strategy needs avg R > 0 and profit factor > 1 AFTER costs.

Example:
    python backtest.py                                       # 10 random NSE tickers, 2y daily
    python backtest.py --count 15 --seed 42                  # reproducible random pick
    python backtest.py RELIANCE TCS INFY                     # explicit symbols
    python backtest.py --longs-only --trailing chandelier    # recommended robust config
    python backtest.py --csv trades.csv                      # also dump per-trade CSV
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd
from rich.console import Console
from rich.table import Table

from analyze import SCRIPT_DIR, analyze_df, fetch

console = Console()


# ──────────────────────────────────────────────────────────────────────────
# Universe — random pool of liquid NSE names.
# Curated rather than purely random because "random delisted penny stock"
# is not what anyone means by "random share". Pool covers NIFTY 50 + Next 50
# + a few liquid midcaps for variety.
# ──────────────────────────────────────────────────────────────────────────
NSE_POOL: list[str] = [
    # NIFTY 50 — large-cap, very liquid
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK",
    "ASIANPAINT", "MARUTI", "BAJFINANCE", "HCLTECH", "WIPRO", "ULTRACEMCO",
    "SUNPHARMA", "TITAN", "NESTLEIND", "POWERGRID", "NTPC", "M&M",
    "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "ADANIENT", "ADANIPORTS",
    "HINDALCO", "ONGC", "COALINDIA", "GRASIM", "DRREDDY", "CIPLA",
    "EICHERMOT", "BAJAJFINSV", "TECHM", "BPCL", "DIVISLAB", "BRITANNIA",
    "HEROMOTOCO", "INDUSINDBK", "SBILIFE", "HDFCLIFE", "TATACONSUM",
    "APOLLOHOSP", "UPL", "BAJAJ-AUTO",
    # NIFTY Next 50 / midcap — for variety
    "DMART", "PIDILITIND", "HAVELLS", "DABUR", "GODREJCP", "MARICO",
    "BERGEPAINT", "SHRIRAMFIN", "AMBUJACEM", "VEDL", "GAIL", "IOC",
    "JINDALSTEL", "SAIL", "MOTHERSON", "BOSCHLTD", "TVSMOTOR", "ASHOKLEY",
    "AUROPHARMA", "BIOCON", "LUPIN", "TORNTPHARM",
    "MUTHOOTFIN", "CHOLAFIN", "BANDHANBNK", "FEDERALBNK", "IDFCFIRSTB",
    "PNB", "BANKBARODA", "IRCTC", "PAGEIND",
    "ICICIPRULI", "ICICIGI", "PERSISTENT", "MPHASIS", "LTIM", "COFORGE",
]


# ──────────────────────────────────────────────────────────────────────────
# Cost model — applied to every round-trip.
# Defaults approximate Zerodha delivery: ~5 bps slippage per side
# + ~0.05% round-trip fees on notional. Conservative for liquid largecaps,
# optimistic for thin midcaps; override with --slippage-bps / --fees-pct.
# ──────────────────────────────────────────────────────────────────────────
DEFAULT_SLIPPAGE_BPS = 5.0      # one side, bps of price
DEFAULT_FEES_PCT = 0.05         # total round-trip fees as % of notional


# ──────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class Trade:
    symbol: str
    side: str                  # 'long' | 'short'
    entry_time: pd.Timestamp
    entry: float
    stop: float
    target: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str           # 'stop' | 'target' | 'timeout'
    score: int
    label: str
    r_multiple: float          # net of costs
    r_gross: float             # before costs

    def as_row(self) -> dict:
        return {
            "symbol": self.symbol, "side": self.side,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "entry": self.entry, "stop": self.stop, "target": self.target,
            "exit_price": round(self.exit_price, 4),
            "exit_reason": self.exit_reason,
            "score": self.score, "label": self.label,
            "r_gross": round(self.r_gross, 3),
            "r_multiple": round(self.r_multiple, 3),
        }


@dataclass
class SymbolReport:
    symbol: str
    trades: list[Trade] = field(default_factory=list)
    bars_seen: int = 0
    skipped_reason: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────────
# Simulation
# ──────────────────────────────────────────────────────────────────────────
def _apply_costs(side: str, entry: float, exit_price: float, stop: float,
                 slippage_bps: float, fees_pct: float) -> tuple[float, float]:
    """Return (r_gross, r_net) given cost assumptions.

    Slippage is applied symmetrically: entry is worse than the model entry,
    exit is worse than the model exit. R-denominator (|entry - stop|) is
    held fixed at the *planned* risk so that R-multiples remain comparable
    across trades.
    """
    risk = abs(entry - stop)
    if risk == 0:
        return 0.0, 0.0
    slip = entry * (slippage_bps / 10_000.0)
    if side == "long":
        adj_entry = entry + slip
        adj_exit = exit_price - slip
        pnl = adj_exit - adj_entry
    else:
        adj_entry = entry - slip
        adj_exit = exit_price + slip
        pnl = adj_entry - adj_exit
    fees = (entry + exit_price) * (fees_pct / 100.0) / 2.0  # symmetric, % of avg notional
    r_gross = (exit_price - entry) / risk if side == "long" else (entry - exit_price) / risk
    r_net = (pnl - fees) / risk
    return r_gross, r_net


def backtest_symbol(
    symbol: str,
    *,
    period: str,
    interval: str,
    min_score: int = 2,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    fees_pct: float = DEFAULT_FEES_PCT,
    max_hold_bars: int = 40,             # force flat after N bars (~ 2 trading months)
    longs_only: bool = False,            # skip short signals (Indian markets are long-biased)
    trailing: str = "fixed",             # 'fixed' (1:3 R:R target) or 'chandelier' (ATR trailing stop)
    chandelier_mult: float = 3.0,        # Chuck LeBeau's classic Chandelier exit multiplier
) -> SymbolReport:
    """Replay the swing strategy across one symbol's daily history.

    Uses analyze_df() — 7 daily voters with the ADX(14) gate; positions can
    hold across many days; force-flat after max_hold_bars.

    Trailing = 'chandelier' replaces the fixed 1:3 R:R target with a Chandelier
    exit (LeBeau): for longs, stop = max(high_since_entry) − 3*ATR; flips up
    as new highs print, never down. Lets winners run beyond 3R; cuts winners
    short who fail to extend. Industry-standard exit for trend systems.
    """
    use_trailing = (trailing == "chandelier")
    min_history = 80  # need warm-up for EMA200

    ticker = symbol if "." in symbol else f"{symbol}.NS"
    df = fetch(ticker, period, interval)
    rep = SymbolReport(symbol=symbol)
    if df is None or len(df) < min_history:
        rep.skipped_reason = "no/insufficient data"
        return rep
    rep.bars_seen = len(df)

    in_trade: Optional[dict] = None
    last_actionable_score = 0        # debounce: only re-enter after signal clears

    start_i = 50  # warm-up for EMA50
    for i in range(start_i, len(df) - 1):
        bar = df.iloc[i]
        next_bar = df.iloc[i + 1]
        cur_window = df.iloc[:i + 1]

        # ── 1. If we're in a trade, see whether next bar's high/low hits stop or target.
        if in_trade is not None:
            side = in_trade["side"]
            hi, lo = float(next_bar["High"]), float(next_bar["Low"])

            # Update trailing levels (Chandelier exit if enabled).
            if use_trailing:
                atr_now = float(next_bar["ATR"]) if "ATR" in next_bar and pd.notna(next_bar["ATR"]) else in_trade["atr_at_entry"]
                if side == "long":
                    in_trade["hi_since"] = max(in_trade["hi_since"], hi)
                    new_trail = in_trade["hi_since"] - chandelier_mult * atr_now
                    if new_trail > in_trade["stop"]:
                        in_trade["stop"] = new_trail
                else:
                    in_trade["lo_since"] = min(in_trade["lo_since"], lo)
                    new_trail = in_trade["lo_since"] + chandelier_mult * atr_now
                    if new_trail < in_trade["stop"]:
                        in_trade["stop"] = new_trail

            stop, target = in_trade["stop"], in_trade["target"]
            hit_reason: Optional[str] = None
            exit_price: float = 0.0

            if side == "long":
                if lo <= stop:
                    hit_reason, exit_price = "stop", stop
                elif target is not None and hi >= target:
                    hit_reason, exit_price = "target", target
            else:  # short
                if hi >= stop:
                    hit_reason, exit_price = "stop", stop
                elif target is not None and lo <= target:
                    hit_reason, exit_price = "target", target

            # max-hold flat-out
            if hit_reason is None and (i + 1) - in_trade["entry_idx"] >= max_hold_bars:
                hit_reason = "timeout"
                exit_price = float(next_bar["Close"])

            if hit_reason is not None:
                # R-multiple uses the ORIGINAL stop so trailing doesn't inflate R.
                r_gross, r_net = _apply_costs(
                    side, in_trade["entry"], exit_price, in_trade["original_stop"],
                    slippage_bps, fees_pct,
                )
                rep.trades.append(Trade(
                    symbol=symbol, side=side,
                    entry_time=in_trade["entry_time"], entry=in_trade["entry"],
                    stop=in_trade["original_stop"], target=in_trade["target"] or 0.0,
                    exit_time=next_bar.name, exit_price=exit_price,
                    exit_reason=hit_reason, score=in_trade["score"],
                    label=in_trade["label"], r_gross=r_gross, r_multiple=r_net,
                ))
                in_trade = None
                last_actionable_score = 0
                continue

        # ── 2. Flat: run the scanner on history up to and including bar `i`.
        if in_trade is None:
            a = analyze_df(symbol, cur_window.copy(), quiet=True)
            if a is None or a.entry is None:
                last_actionable_score = 0
                continue
            if abs(a.total) < min_score:
                last_actionable_score = 0
                continue
            if last_actionable_score != 0 and (last_actionable_score * a.total) > 0:
                continue
            side = "long" if a.total > 0 else "short"
            if longs_only and side == "short":
                continue
            entry = float(next_bar["Open"])
            original_stop = float(a.stop)
            target_val = None if use_trailing else float(a.target)
            atr_at_entry = float(bar["ATR"]) if "ATR" in bar.index and pd.notna(bar["ATR"]) else 0.0
            in_trade = {
                "side": side, "entry": entry,
                "stop": original_stop, "original_stop": original_stop,
                "target": target_val,
                "entry_time": next_bar.name,
                "entry_idx": i + 1,
                "score": a.total, "label": a.label,
                "atr_at_entry": atr_at_entry,
                "hi_since": entry,
                "lo_since": entry,
            }
            last_actionable_score = a.total

    # ── 3. If still in a trade at the very end, mark-to-market on last close.
    if in_trade is not None:
        last_bar = df.iloc[-1]
        exit_price = float(last_bar["Close"])
        r_gross, r_net = _apply_costs(
            in_trade["side"], in_trade["entry"], exit_price, in_trade["original_stop"],
            slippage_bps, fees_pct,
        )
        rep.trades.append(Trade(
            symbol=symbol, side=in_trade["side"],
            entry_time=in_trade["entry_time"], entry=in_trade["entry"],
            stop=in_trade["original_stop"], target=in_trade["target"] or 0.0,
            exit_time=last_bar.name, exit_price=exit_price,
            exit_reason="timeout", score=in_trade["score"],
            label=in_trade["label"], r_gross=r_gross, r_multiple=r_net,
        ))

    return rep


# ──────────────────────────────────────────────────────────────────────────
# Stats + rendering
# ──────────────────────────────────────────────────────────────────────────
def aggregate_stats(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    rs = [t.r_multiple for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    win_rate = len(wins) / len(rs)
    total_r = sum(rs)
    avg_r = total_r / len(rs)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    profit_factor = (sum(wins) / -sum(losses)) if sum(losses) < 0 else float("inf") if wins else 0.0

    # equity curve in R + max drawdown
    eq, peak, dd, max_dd = 0.0, 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        if eq > peak:
            peak = eq
        dd = eq - peak
        if dd < max_dd:
            max_dd = dd

    target_hits  = sum(1 for t in trades if t.exit_reason == "target")
    stop_hits    = sum(1 for t in trades if t.exit_reason == "stop")
    timeout_hits = sum(1 for t in trades if t.exit_reason == "timeout")

    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "avg_r": avg_r,
        "avg_win_r": avg_win,
        "avg_loss_r": avg_loss,
        "total_r": total_r,
        "profit_factor": profit_factor,
        "max_dd_r": max_dd,
        "target_hits": target_hits,
        "stop_hits": stop_hits,
        "timeout_hits": timeout_hits,
    }


def render_per_symbol(reports: list[SymbolReport]):
    t = Table(title="Per-symbol results", header_style="bold")
    t.add_column("Symbol", style="cyan")
    t.add_column("Bars", justify="right")
    t.add_column("Trades", justify="right")
    t.add_column("Win%", justify="right")
    t.add_column("Avg R", justify="right")
    t.add_column("Total R", justify="right")
    t.add_column("PF", justify="right")
    t.add_column("MaxDD (R)", justify="right")
    t.add_column("Hits (T/S/TO)", justify="right")

    for r in reports:
        if r.skipped_reason:
            t.add_row(r.symbol, "—", "—", "—", "—", "—", "—", "—",
                      f"[dim]{r.skipped_reason}[/dim]")
            continue
        s = aggregate_stats(r.trades)
        if s["trades"] == 0:
            t.add_row(r.symbol, str(r.bars_seen), "0", "—", "—", "—", "—", "—", "—")
            continue
        avg_r_color = "green" if s["avg_r"] > 0 else "red"
        tot_r_color = "green" if s["total_r"] > 0 else "red"
        pf_disp = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "∞"
        t.add_row(
            r.symbol, str(r.bars_seen), str(s["trades"]),
            f"{s['win_rate']*100:.0f}%",
            f"[{avg_r_color}]{s['avg_r']:+.2f}[/{avg_r_color}]",
            f"[{tot_r_color}]{s['total_r']:+.2f}[/{tot_r_color}]",
            pf_disp,
            f"{s['max_dd_r']:+.2f}",
            f"{s['target_hits']}/{s['stop_hits']}/{s['timeout_hits']}",
        )
    console.print(t)


def render_overall(all_trades: list[Trade], risk_inr: float):
    if not all_trades:
        console.print("[yellow]No trades generated. Try a longer --period, lower --min-score, or a different sample.[/yellow]")
        return
    s = aggregate_stats(all_trades)
    inr_pnl = s["total_r"] * risk_inr

    t = Table(title="Aggregate", header_style="bold", show_header=False)
    t.add_column("Metric", style="cyan")
    t.add_column("Value")

    def color(v, good_when):
        good = good_when(v)
        c = "green" if good else "red"
        return f"[{c}]{v}[/{c}]"

    t.add_row("Trades", str(s["trades"]))
    t.add_row("Win rate", f"{s['win_rate']*100:.1f}%")
    t.add_row("Avg R (net of costs)", color(f"{s['avg_r']:+.3f}", lambda v: float(v) > 0))
    t.add_row("Avg win / avg loss", f"{s['avg_win_r']:+.2f}R  /  {s['avg_loss_r']:+.2f}R")
    pf_str = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "∞"
    t.add_row("Profit factor", color(pf_str, lambda v: v == "∞" or float(v) > 1.0))
    t.add_row("Total R", color(f"{s['total_r']:+.2f}", lambda v: float(v) > 0))
    t.add_row(f"P&L @ ₹{risk_inr:,.0f}/trade", color(f"₹{inr_pnl:+,.0f}", lambda v: not v.startswith("[red") and "-" not in v))
    t.add_row("Max drawdown (R)", f"{s['max_dd_r']:+.2f}")
    t.add_row("Exits T / S / Timeout", f"{s['target_hits']} / {s['stop_hits']} / {s['timeout_hits']}")
    console.print(t)

    edge = s["avg_r"] > 0 and s["profit_factor"] > 1.0
    if edge:
        console.print("[bold green]→ Apparent positive edge on this sample. Replicate on a different random seed before believing it.[/bold green]")
    else:
        console.print("[bold red]→ No positive edge on this sample. Do NOT auto-execute this strategy.[/bold red]")


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", help="explicit NSE symbols; overrides --count if given")
    ap.add_argument("--count", type=int, default=10, help="random sample size from the NSE pool (default 10)")
    ap.add_argument("--seed", type=int, default=None, help="random seed for reproducibility")
    ap.add_argument("-i", "--interval", default="1d", choices=["1d", "1wk"],
                    help="candle interval (default: 1d)")
    ap.add_argument("--period", default="2y",
                    help="yfinance lookback (default: 2y)")
    ap.add_argument("--min-score", type=int, default=2,
                    help="minimum |score| to take a trade (default 2 = BUY/SELL threshold)")
    ap.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    ap.add_argument("--fees-pct", type=float, default=DEFAULT_FEES_PCT,
                    help="round-trip fees as %% of notional (default 0.05)")
    ap.add_argument("--risk-inr", type=float, default=1000.0,
                    help="₹ risk per trade for the rupee-equivalent P&L line (default 1000)")
    ap.add_argument("--csv", type=Path, default=None, help="dump per-trade CSV to this path")
    ap.add_argument("--longs-only", action="store_true",
                    help="skip short trades (Indian markets are structurally long-biased)")
    ap.add_argument("--trailing", choices=["fixed", "chandelier"], default="fixed",
                    help="exit method: fixed 1:3 target, or Chandelier ATR-trailing stop")
    ap.add_argument("--chandelier-mult", type=float, default=3.0,
                    help="Chandelier ATR multiplier (LeBeau default: 3.0)")
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        rng = random.Random(args.seed)
        symbols = rng.sample(NSE_POOL, k=min(args.count, len(NSE_POOL)))

    flags = []
    if args.longs_only:           flags.append("longs-only")
    if args.trailing != "fixed":  flags.append(f"trailing={args.trailing}({args.chandelier_mult}×ATR)")
    flag_str = "  ·  " + "  ·  ".join(flags) if flags else ""
    console.rule(
        f"[bold]Backtest  •  swing{flag_str}  •  {len(symbols)} symbol(s)  •  "
        f"{args.interval} candles  •  period {args.period}  •  min |score| {args.min_score}[/bold]"
    )
    console.print(
        f"Universe: [cyan]{', '.join(symbols)}[/cyan]\n"
        f"Costs: {args.slippage_bps:.1f} bps slippage per side  +  "
        f"{args.fees_pct:.2f}% round-trip fees\n"
        f"Risk per trade: ₹{args.risk_inr:,.0f}  (used only for the ₹ summary)\n"
    )

    reports: list[SymbolReport] = []
    t0 = time.perf_counter()
    for s in symbols:
        console.print(f"  [dim]· running {s}…[/dim]")
        try:
            rep = backtest_symbol(
                s, period=args.period, interval=args.interval,
                min_score=args.min_score,
                slippage_bps=args.slippage_bps,
                fees_pct=args.fees_pct,
                longs_only=args.longs_only,
                trailing=args.trailing,
                chandelier_mult=args.chandelier_mult,
            )
        except Exception as e:  # noqa: BLE001
            rep = SymbolReport(symbol=s, skipped_reason=f"error: {type(e).__name__}")
            console.print(f"    [red]error[/red]: {e}")
        reports.append(rep)

    elapsed = time.perf_counter() - t0
    console.print(f"\n[dim]ran {len(symbols)} symbols in {elapsed:.1f}s[/dim]\n")

    render_per_symbol(reports)

    all_trades = [t for r in reports for t in r.trades]
    render_overall(all_trades, args.risk_inr)

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(Trade.__annotations__.keys()))
            w.writeheader()
            for tr in all_trades:
                w.writerow(tr.as_row())
        console.print(f"\n[dim]wrote {len(all_trades)} trades → {args.csv}[/dim]")

    console.print(
        "\n[dim]Caveats: survivorship bias in NIFTY-50-style pools; overnight "
        "gaps DOWN through a stop are modelled as filled at the stop level here "
        "but in reality fill at the gap-open price; no corporate-action handling.[/dim]"
    )


if __name__ == "__main__":
    main()
