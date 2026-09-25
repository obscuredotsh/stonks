# stocks — NSE swing signal scanner

Tiny CLI + web app that pulls **daily candles** from Yahoo Finance and prints
a per-stock buy/sell signal based on **seven independent voters**:

- **EMA 20/50** — short/medium trend (golden / death cross)
- **EMA 200** — long-term stage filter
- **RSI(14)** — momentum
- **MACD(12,26,9)** — momentum-continuation
- **Bollinger(20,2)** — volatility / breakout
- **Vol + 20d breakout** — confirmation
- **Supertrend(10, 3.0)** — dynamic trend filter (popular in Indian retail)

…plus an **ADX(14) trend-strength gate**: if ADX < 20 the market is choppy,
so the analyzer suppresses entry / stop / target levels even on a strong vote.

Each voter casts −1, 0, or +1. The sum maps to **STRONG BUY → STRONG SELL**.
For actionable signals (|score| ≥ 2 and ADX ≥ 20) the tool emits an
ATR-based stop and a **1:3 R:R** target.

A **screener** ranks an NSE universe (`universe.txt`) by composite
momentum + liquidity + jumpiness and auto-rewrites `watchlist.txt` with
the top N picks so the live scanner / watcher / web UI always work on
the highest-momentum names you care about.

## Quick start

Requires Python 3.10+ and an internet connection (Yahoo Finance is the data source — no API key needed).

### 1. First-time setup (run once)

```bash
cd ~/code/stocks
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Start the web app (every time)

```bash
cd ~/code/stocks
source .venv/bin/activate      # skip if your venv is already active
python app.py
```

Then open <http://127.0.0.1:5050> in your browser. The home page is the
scanner UI; `/docs` renders this guide and the long-form `documentation.md`.

To stop the server: hit `Ctrl-C` in the terminal running `python app.py`
(or `pkill -f "python app.py"` from another shell).

### 3. Keep the watchlist fresh (screener)

`screener.py` ranks every name in `universe.txt` (≈150 liquid NSE
candidates curated for you) on a composite z-score of:

- **Momentum** (55%): 1-month return + 3-month return + closeness to 52-week high.
- **Liquidity** (25%): log of avg-daily turnover (in ₹ crore) over last 20 sessions.
- **Jumpiness** (20%): ATR/price + biggest 5-day move in last 20 sessions.

It filters out penny stocks (< ₹50), illiquid names (< ₹5 cr daily
turnover), and busted stocks (> 40% below 52-week high), then writes
the top N to `watchlist.txt`:

```bash
python screener.py                          # top 15 → watchlist.txt
python screener.py --top 25
python screener.py --side short             # rank by downtrend strength
python screener.py --w-momentum 0.7 --w-jump 0.3   # weight the factors yourself
python screener.py --dry-run                # show ranking only
```

Edit `universe.txt` to add/remove candidates. Run the screener at the
end of each trading day so the watchlist always reflects today's
momentum picture; everything downstream (CLI, web UI, watcher, backtester)
reads `watchlist.txt` so it picks up new names automatically.

### 4. (Optional) Use the CLI instead

```bash
source .venv/bin/activate

# scan watchlist.txt — daily candles, 1y history
python analyze.py

# specific tickers
python analyze.py RELIANCE TCS INFY

# filter to actionable trades only
python analyze.py --only buy
python analyze.py --only sell

# weekly candles for longer-term swings
python analyze.py --interval 1wk --period 2y
```

### 5. Live mode in the web UI (with stop / target alerts)

In the scanner page the **▶ Live** button (next to *Scan*) turns the page
into a self-refreshing dashboard at the cadence you pick (60s – 1h). When
it's running:

- Every actionable signal that appears is *frozen* into a "Tracking" panel
  with its entry / stop / target captured at first sight.
- On every refresh, the latest price is compared to those frozen levels.
  When the **stop** or **target** is reached, a macOS Notification Center
  alert fires (via the same `notify.py` used by the watcher) and the row
  is badged 🛑 STOP / 🎯 TARGET.
- Tracked positions persist in `localStorage`, so a browser refresh
  doesn't lose them. The **Clear** button on the panel resets them.
- A pulsing red **LIVE** pill in the header tells you the loop is on; click
  **⏸ Pause** to stop.

This is the in-browser counterpart to `watch.py`. Use the UI when the page
is open; use the watcher when you want signal-transition alerts to keep
firing in the background.

### 6. (Optional) Desktop notifications via the watcher

`watch.py` runs the scanner on a loop and fires a macOS Notification Center
alert whenever a stock's signal **changes bucket** (e.g. HOLD → BUY,
BUY → STRONG BUY, BUY → SELL). State is kept in `.watch_state.json` so
restarting the watcher doesn't re-fire stale alerts.

```bash
source .venv/bin/activate

# poll watchlist.txt every 30 min (default), mac notifications on transitions
python watch.py

# explicit symbols, faster polling (e.g. 5 min)
python watch.py --every 300 CUPID RELIANCE TCS

# one-shot scan + notify (good for end-of-day cron / launchd)
python watch.py --once

# notify on every actionable scan, not just transitions
python watch.py --all

# console-only, no popups (debugging)
python watch.py --no-notify
```

Each notification looks like:

```
↑ RELIANCE — STRONG BUY
₹1436.00  ·  score +5
Entry 1436.0  ·  Stop 1418.0  ·  Target 1472.0
long swing  ·  ADX 32  ·  2×ATR stop  ·  1:3 R:R
```

Test the notification pipeline by itself:

```bash
python notify.py "stocks · test" "If you can see this, you're wired up." "score +5"
```

For prettier notifications (subtitle, click-to-focus) install the optional
`terminal-notifier` binary — `notify.py` will pick it up automatically:

```bash
brew install terminal-notifier
```

### 7. Backtest before you trust it

```bash
source .venv/bin/activate

# default: 2y of daily candles, 10 random NSE names
python backtest.py
python backtest.py --count 20 --seed 42

# explicit symbols (positional args)
python backtest.py RELIANCE TCS INFY

# robust config — recommended for any paper-trade session
python backtest.py --count 15 --seed 42 --longs-only --trailing chandelier

# only strongest signals + dump CSV
python backtest.py --min-score 3 --csv /tmp/trades.csv
```

Reports results in **R-multiples** (1R = planned per-trade risk) plus a
₹-equivalent P&L. Walk-forward simulation, no look-ahead, realistic
slippage and fees.

**Latest A/B results on a 15-symbol random sample, two seeds:**

| Variant | Trades | Win % | Avg R | Profit factor |
| --- | ---: | ---: | ---: | ---: |
| Baseline (no flags, seed 42) | 254 | 31.5% | −0.027 | 0.96 |
| **`--longs-only --trailing chandelier` (seed 42)** | **881** | **69.4%** | **+0.359** | **34.6** |
| **`--longs-only --trailing chandelier` (seed 7, held-out)** | **956** | **70.7%** | **+0.285** | **29.5** |

The three "popular-method" upgrades (**ADX(14) gate**, **Supertrend(10,3)
voter**, **Chandelier trailing exit** + **longs-only**) flip the edge
from negative to clearly positive on both seeds. The headline PF of 30+
is **not** what you'll see live — at 5× realistic friction (25 bps
slippage + 0.20 % fees) the same config nets PF 4.3 and +0.21R/trade,
which is the more honest expectation. See `documentation.md` §10 for
the full ablation, stress-test, and the "why this isn't free money"
caveats.

### 8. Other notification channels (Telegram, Slack, …)

`notify.py` defines a tiny `Notifier` protocol and ships a `MacNotifier`,
`ConsoleNotifier`, and a stub `TelegramNotifier`. To enable Telegram alerts,
set two env vars and add the notifier to the watcher's list:

```bash
export TELEGRAM_BOT_TOKEN="123456:abcdef..."   # from @BotFather
export TELEGRAM_CHAT_ID="123456789"            # your numeric chat id
```

Then in `watch.py` (or a small wrapper script):

```python
from notify import default_notifiers, TelegramNotifier
notifiers = default_notifiers() + [TelegramNotifier.from_env()]
```

Adding Slack / ntfy.sh / email is the same pattern: subclass nothing, just
write a class with a `name` attribute and a `send(title, message, subtitle)`
method, then append an instance to the notifiers list.

### Troubleshooting

- **`ModuleNotFoundError: No module named 'flask'` / `yfinance`** — your
  virtualenv isn't active. Re-run `source .venv/bin/activate`, then
  `pip install -r requirements.txt`.
- **Port 5050 already in use** — either stop the other process
  (`lsof -i :5050`) or run on a different port:
  `FLASK_RUN_PORT=5051 flask --app app run` (or edit the `port=` in `app.py`).
- **Empty / "no data" results** — Yahoo throttles or the symbol is wrong.
  NSE tickers are auto-suffixed with `.NS`; double-check spelling in
  `watchlist.txt`.

## Disclaimer

Educational / research only. This is a technical scanner, not financial
advice. Always verify with your own analysis and risk plan.
