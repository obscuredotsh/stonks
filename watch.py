#!/usr/bin/env python3
"""Long-running watcher that fires desktop notifications on signal changes.

Polls the watchlist on a schedule, runs the same `analyze.analyze()` the
CLI / web UI use, and pushes a macOS notification when a stock's signal
moves into or between actionable buckets (BUY, STRONG BUY, SELL, STRONG SELL).

State is persisted to `.watch_state.json` next to this file so restarting the
watcher doesn't re-fire alerts for signals that haven't changed.

Examples:
    python watch.py                       # momentum_picks.txt, daily candles, every 30 min, mac notifs
    python watch.py CUPID RELIANCE TCS    # explicit symbols
    python watch.py --every 1800          # poll every 30 minutes (default)
    python watch.py --once                # run a single pass and exit (good for cron)
    python watch.py --all                 # notify every actionable scan, not just transitions
    python watch.py --no-notify           # console-only (good for sanity checks)
    python watch.py --include-hold        # also ping HOLD/WAIT (default: suppressed)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from analyze import SCRIPT_DIR, Analysis, analyze, load_watchlist
from notify import ConsoleNotifier, Notifier, default_notifiers

STATE_PATH = SCRIPT_DIR / ".watch_state.json"

# Maps the labels emitted by analyze.classify() to a strength bucket.
# Higher absolute value = stronger conviction. We notify when the bucket
# changes (e.g. HOLD → BUY, BUY → STRONG BUY, BUY → HOLD, BUY → SELL).
_BUCKET = {
    "STRONG SELL": -2,
    "SELL": -1,
    "HOLD": 0,
    "WAIT": 0,
    "HOLD / WAIT": 0,
    "BUY": 1,
    "STRONG BUY": 2,
}


def label_bucket(label: str) -> int:
    return _BUCKET.get(label.strip().upper(), 0)


def is_actionable(label: str) -> bool:
    return abs(label_bucket(label)) >= 1


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def should_notify(
    prev_label: str | None,
    new_label: str,
    *,
    transitions_only: bool,
    include_hold: bool,
) -> bool:
    new_bucket = label_bucket(new_label)
    if not include_hold and new_bucket == 0:
        return False  # never wake the user up to say "do nothing"
    if not transitions_only:
        return is_actionable(new_label) or include_hold
    if prev_label is None:
        return new_bucket != 0
    return label_bucket(prev_label) != new_bucket


def format_alert(a: Analysis) -> tuple[str, str, str]:
    """Build (title, subtitle, message) for the notification."""
    arrow = "↑" if a.total > 0 else ("↓" if a.total < 0 else "·")
    title = f"{arrow} {a.symbol} — {a.label}"
    subtitle = f"₹{a.last_price:.2f}  ·  score {a.total:+d}"
    if a.entry is not None and a.stop is not None and a.target is not None:
        message = (
            f"Entry {a.entry}  ·  Stop {a.stop}  ·  Target {a.target}\n"
            f"{a.note}"
        )
    else:
        message = a.note or "watch for clearer alignment"
    return title, subtitle, message


def run_once(
    symbols: list[str],
    *,
    interval: str,
    period: str,
    notifiers: list[Notifier],
    state: dict,
    transitions_only: bool,
    include_hold: bool,
) -> None:
    for sym in symbols:
        try:
            a = analyze(sym, period=period, interval=interval)
        except Exception as e:  # noqa: BLE001
            print(f"  [error] {sym}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        if a is None:
            continue

        prev = state.get(sym, {}).get("label")
        fire = should_notify(
            prev, a.label, transitions_only=transitions_only, include_hold=include_hold
        )
        marker = "*" if fire else " "
        print(
            f"  {marker} {a.symbol:<12} {a.label:<13} "
            f"score {a.total:+d}  ₹{a.last_price:.2f}  "
            f"(prev: {prev or '—'})"
        )

        if fire:
            title, subtitle, message = format_alert(a)
            for n in notifiers:
                try:
                    n.send(title, message, subtitle=subtitle)
                except Exception as e:  # noqa: BLE001
                    print(f"  [notifier {n.name} failed]: {e}", file=sys.stderr)

        state[sym] = {
            "label": a.label,
            "score": a.total,
            "price": a.last_price,
            "ts": a.last_time.isoformat(),
        }

    save_state(state)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("symbols", nargs="*", help="NSE tickers; overrides momentum_picks.txt if given")
    p.add_argument("-w", "--watchlist", type=Path, default=SCRIPT_DIR / "momentum_picks.txt")
    p.add_argument("-i", "--interval", default="1d",
                   choices=["1d", "1wk"],
                   help="candle interval (default 1d)")
    p.add_argument("--period", default="1y", help="yfinance lookback (default 1y)")
    p.add_argument("--every", type=int, default=1800,
                   help="seconds between polls (default 1800 = 30 min)")
    p.add_argument("--once", action="store_true",
                   help="run a single pass then exit (good for cron / testing)")
    p.add_argument("--all", action="store_true",
                   help="notify on every actionable scan, not just on transitions")
    p.add_argument("--include-hold", action="store_true",
                   help="also fire notifications for HOLD / WAIT (default suppresses them)")
    p.add_argument("--no-notify", action="store_true",
                   help="console-only output, no desktop notifications")
    p.add_argument("--reset-state", action="store_true",
                   help="clear .watch_state.json before starting")
    args = p.parse_args()

    syms = args.symbols or load_watchlist(args.watchlist)
    if not syms:
        print("No symbols. Pass them on the CLI or populate momentum_picks.txt.", file=sys.stderr)
        sys.exit(2)

    notifiers: list[Notifier]
    if args.no_notify:
        notifiers = [ConsoleNotifier()]
    else:
        notifiers = default_notifiers()

    if args.reset_state and STATE_PATH.exists():
        STATE_PATH.unlink()
    state = load_state()

    print(
        f"watching {len(syms)} symbol(s) every {args.every}s "
        f"({args.interval} candles, period {args.period}); "
        f"notifiers: {[n.name for n in notifiers]}"
    )
    print(f"state file: {STATE_PATH}")
    print("press Ctrl-C to stop.\n")

    try:
        while True:
            print(f"— scan {datetime.now():%H:%M:%S} —")
            run_once(
                syms,
                interval=args.interval,
                period=args.period,
                notifiers=notifiers,
                state=state,
                transitions_only=not args.all,
                include_hold=args.include_hold,
            )
            if args.once:
                break
            time.sleep(args.every)
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
