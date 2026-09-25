# Stocks — High-Level Design & Beginner's Guide

A small personal tool that pulls **daily** NSE candles, runs them through
seven classic technical indicators with an ADX(14) trend-strength gate,
and combines the votes into a single buy / sell signal — viewable from a
CLI **or** a one-page web UI.

> **Disclaimer.** This is a learning project, not financial advice. Indicators
> describe the past; markets are noisy and signals fail often. Never put money
> on a trade you don't understand or can't afford to lose. Stop. Read this
> whole doc once before clicking anything.

---

## 1. What problem this solves

When you open a chart on TradingView or your broker, you see candles, dozens
of indicators, and noise. As a beginner, two questions are paralysing:

1. **Is this stock biased up or down right now?**
2. **If I take a trade, where do I get out?**

This tool answers both:

- It computes seven independent indicators, lets each one "vote" −1 / 0 / +1, and
  publishes a composite score → **STRONG BUY / BUY / HOLD / SELL / STRONG SELL**.
- It runs an **ADX(14) gate**: if the market isn't actually trending,
  the analyzer suppresses entry levels even when the vote looks actionable.
- For any actionable score (|score| ≥ 2 with ADX ≥ 20) it suggests **Entry**,
  **Stop-loss**, and **Target** prices using ATR (volatility-scaled) and a
  fixed **1:3 reward-to-risk** ratio.

Think of it as a second opinion that disagrees with itself out loud.

---

## 2. High-Level Design

```
┌─────────────────────┐        ┌─────────────────────────┐
│  watchlist.txt      │        │  CLI: python analyze.py │
│  (NSE symbols)      │        └────────────┬────────────┘
└──────────┬──────────┘                     │
           │                                ▼
           │                      ┌─────────────────────┐
           ▼                      │   analyze.analyze() │   pure-pandas
┌──────────────────────┐  ─────►  │   • fetch (yfinance)│   indicators
│  Browser  ──►  /     │          │   • indicators      │   (no native
│           ◄── HTML   │          │   • voters          │    deps, no
│                      │          │   • score → label   │    API keys)
│           ──► /api/  │          │   • entry/stop/tgt  │
│           ◄── JSON   │  ─────►  └──────────┬──────────┘
└──────────────────────┘                     │
           ▲                                 ▼
           │                      ┌─────────────────────┐
           │                      │  Yahoo Finance      │
           └──────────────────────┤  query1.../chart    │
                                  │  (free, no key)     │
                                  └─────────────────────┘
```

### Components

| Layer            | File                       | Responsibility                                                                                       |
| ---------------- | -------------------------- | ---------------------------------------------------------------------------------------------------- |
| Data source      | `yfinance`                 | Free OHLCV from Yahoo Finance. NSE tickers use the `.NS` suffix (auto-appended).                     |
| Signal engine    | `analyze.py`               | Pure pandas/numpy. Computes EMA, RSI, MACD, ATR, Bollinger, ADX, Supertrend. Decides votes + levels. |
| CLI renderer     | `analyze.py` (`render`)    | `rich`-formatted tables for terminal use.                                                            |
| Watchlist        | `watchlist.txt`            | Plain text, one symbol per line, `#` comments allowed.                                               |
| Screener         | `screener.py`              | Ranks `universe.txt` by composite momentum + liquidity + jumpiness; rewrites `watchlist.txt`.        |
| Web server       | `app.py`                   | Flask. `GET /` (UI), `POST /api/scan` (JSON), `POST /api/notify`, `GET /docs`.                       |
| Front-end        | `templates/index.html`     | Single-file dark-themed page. Vanilla JS, no build step.                                             |
| Backtester       | `backtest.py`              | Walk-forward simulator with R-multiples, ADX gate, longs-only, Chandelier trailing exit.             |
| Notifications    | `notify.py` + `watch.py`   | macOS Notification Center bridge + long-running scanner that fires on signal transitions.            |
| Dependencies     | `requirements.txt`         | `yfinance`, `pandas`, `numpy`, `rich`, `flask`, `markdown`. Everything else is stdlib.               |

### Data flow (web)

1. Browser opens `GET /` → Flask renders `index.html` with the watchlist seeded
   into the textarea.
2. JavaScript collects symbols + interval + period and `POST`s `/api/scan`.
3. Flask calls `analyze()` once per symbol; each call:
   - Pulls *N* of daily candles via `yfinance.download` (default 1 year).
   - Computes all seven indicators + ADX on the whole series.
   - Looks at the **latest bar** for the votes; the ADX gate runs on the same bar.
   - Returns an `Analysis` dataclass.
4. Flask serialises the list to JSON.
5. Browser sorts by score and renders summary table + per-stock cards.

### The seven voters

| Voter             | Range  | What it captures                                                                          |
| ----------------- | ------ | ----------------------------------------------------------------------------------------- |
| **EMA 20 / 50**   | −1..+1 | Short/medium trend. Golden / death cross fires extra.                                     |
| **EMA 200**       | −1..+1 | Long-term stage filter — Mark Minervini-style up/down regime check.                       |
| **RSI(14)**       | −1..+1 | Momentum. Overbought (>70) = −1, oversold (<30) = +1, mild bias 45–55 = 0.                |
| **MACD(12,26,9)** | −1..+1 | Histogram direction & sign-change. Bullish cross / falling negative / etc.                |
| **Bollinger 20,2**| −1..+1 | Continuation read: breakout above/below bands = direction; inside + rising mid = mild +1. |
| **Vol + Break**   | −1..+1 | 20-day breakout / breakdown on > 1.3× average volume.                                     |
| **SuperTrend**    | −1..+1 | Supertrend(10, 3.0) ATR-band direction; trend flips count extra.                          |

Sum range = −7 .. +7. Mapping (`classify` scales `STRONG` to `voters - 1`):

| Score      | Label         |
| ---------- | ------------- |
| ≥ +6       | STRONG BUY    |
| +2 to +5   | BUY           |
| −1 to +1   | HOLD / WAIT   |
| −5 to −2   | SELL          |
| ≤ −6       | STRONG SELL   |

### The ADX(14) gate

Independently of the vote sum, the analyzer computes ADX(14) (Wilder).
If ADX < 20 the market is choppy / sideways and trend-following systems
have no edge there — so the analyzer suppresses **entry/stop/target**
(sets them to `None`) even on an actionable score. The label still
shows in the UI / CLI, so you can see why the vote was rejected, but
nothing downstream (web UI's Live tracker, watcher, backtester) will
open a position.

### Risk levels (only printed when |score| ≥ 2 AND ADX ≥ 20)

- **Entry** = the latest close.
- **Stop** = `entry − 2 × ATR(14)` for longs (mirror for shorts). ATR is a
  volatility ruler — this gives the trade enough room to breathe without
  taking forever to fail.
- **Target** = `entry + 3 × (entry − stop)` → fixed **1 : 3 reward-to-risk**.
  If you win 3 out of 10, you still profit. (The backtester can also be
  run with `--trailing chandelier` which replaces the fixed target with a
  Chandelier ATR-trailing stop; see §10.)

---

## 3. Beginner's glossary (read once, refer back as needed)

- **Swing trade** = a position held for days to weeks, exited based on
  price action (stop hit, target hit) rather than a daily clock.
- **Candle / bar** = price summary for one time-slice (open, high, low, close,
  volume). A "daily candle" covers one trading session.
- **Long** = bet the price goes up. **Short** = bet the price goes down.
- **Entry** = the price you transact at to open the trade.
- **Stop-loss (stop)** = the price at which you exit a losing trade *automatically*.
  Non-negotiable. Decide before you enter.
- **Target** = the price at which you take profit.
- **R (risk)** = `|entry − stop|`. A "1:3" trade aims to make 3R for every 1R risked.
- **EMA / MA** = (exponential) moving average. Smooths price into a trend line.
- **RSI** = momentum oscillator (0–100). Above 70 = stretched up, below 30 = stretched down.
- **MACD** = trend/momentum indicator built from EMA differences; histogram
  going positive = bullish momentum, negative = bearish.
- **Bollinger Bands** = a 20-period MA with bands ± 2 standard deviations.
- **ATR** = average true range, i.e. typical bar size. Used to size stops.
- **ADX** = Wilder's average directional index. Measures trend *strength*
  (not direction). < 20 = choppy / sideways, > 25 = solid trend.
- **Supertrend** = ATR-based trailing line. Above the line = uptrend, below = downtrend.
- **Chandelier exit** = a trailing stop set 3 × ATR below the highest high
  since entry. Lets winners run; locks in gains once trend reverses.

If any of those still feel fuzzy, search "Investopedia <term>" before trading.

---

## 4. Using the UI as a beginner

### 4.1 Start the app

**First time only** — create the virtualenv and install deps:

```bash
cd ~/code/stocks
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Every subsequent run:**

```bash
cd ~/code/stocks
source .venv/bin/activate      # skip if already active
python app.py
# → open http://127.0.0.1:5050 in your browser
#   (this very page is also served at http://127.0.0.1:5050/docs)
```

Leave the terminal running. To stop the app: hit `Ctrl-C`, or
`pkill -f "python app.py"` from another shell.

If port `5050` is already in use, edit the `app.run(..., port=5050)` line at
the bottom of `app.py` to a free port (e.g. `5051`) and restart.

### 4.2 Anatomy of the page

```
┌─────────────────────────────────────────────────────────────────────┐
│ NSE swing scanner · 7 voters · ADX gate          last scan 16:03:14 │
├─────────────────────────────────────────────────────────────────────┤
│ ┌─Symbols─────────────┐ ┌Interval┐ ┌─Period─┐ ┌──Scan─┐ ┌─▶ Live─┐  │
│ │ BHEL                │ │  1d  ▾ │ │  1y  ▾ │ │ Scan  │ │  5m ▾  │  │
│ │ RELIANCE            │ └────────┘ └────────┘ └───────┘ └────────┘  │
│ │ TCS                 │                                              │
│ └─────────────────────┘                                              │
├─────────────────────────────────────────────────────────────────────┤
│ Summary  (sorted high-to-low score)                                 │
│ Symbol    Price     Score   Signal       Entry / Stop / Target      │
│ BHEL     ₹404.60     +4     BUY          404.6 / 375.62 / 491.54    │
│ RELIANCE ₹1438.70    +2     BUY          1438.7 / 1395 / 1569.7     │
│ ICICIGI  ₹1828.00    +1     HOLD         — (ADX 16, choppy regime)  │
├─────────────────────────────────────────────────────────────────────┤
│ ┌─BHEL  ₹404.60 ────────── BUY ─┐  ┌─ICICIGI ₹1828.00 ─── HOLD ──┐  │
│ │ EMA20/50    +1  20 above 50    │ │ EMA20/50  -1  20 below 50   │  │
│ │ EMA200      +1  above 200d     │ │ EMA200    -1  below 200d    │  │
│ │ RSI(14)     -1  overbought 84  │ │ RSI(14)   +1  bullish 55    │  │
│ │ MACD        +1  rising +ve     │ │ MACD      +1  rising +ve    │  │
│ │ BB(20,2)    +1  rising mid     │ │ BB(20,2)  +1  rising mid    │  │
│ │ Vol+Break    0  0.7x avg       │ │ Vol+Break -1  1.8x down bar │  │
│ │ SuperTrend  +1  up · trail @…  │ │ SuperTrend +1  up · trail   │  │
│ │ ── Entry / Stop / Target ──    │ │ ADX 16 < 20 — choppy, skip  │  │
│ └────────────────────────────────┘ └─────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 4.3 Step-by-step walkthrough

1. **Pick your watchlist.**
   Either edit `watchlist.txt` once (one ticker per line, NSE symbols like
   `BHEL`, `TCS`, `RELIANCE`) or paste tickers directly into the textarea.
   Comma-separated also works. Better still: run `python screener.py`
   first and let the screener pick the highest-momentum names for you.

2. **Pick a timeframe.**
   - *Default:* `Interval = 1d`, `Period = 1y`. Right for almost everyone.
   - *Longer-horizon:* `Interval = 1wk`, `Period = 2y` (weekly candles =
     even slower signals, multi-week holds).
   - *Shorter lookback for recent context:* keep `1d` but switch to `3mo` or `6mo`.

3. **Click Scan.** First scan takes a couple of seconds per symbol (Yahoo is the
   bottleneck). The summary table fills in, sorted by score; cards render below.

4. **Read the summary first.**
   - Look at the **Score** column. ≥ +2 = bullish bias; ≤ −2 = bearish bias.
   - Anything in the **HOLD / WAIT** band is just noise — **do nothing**.
   - The most reliable colour is the deep-green **STRONG BUY** and deep-red
     **STRONG SELL** badges (6 or 7 voters agreeing).
   - If the Entry / Stop / Target column is `—` even though the score is ≥ +2,
     it means **the ADX gate killed it** (choppy market). Check the card for
     the `ADX <n> < 20` note.

5. **Open the corresponding card to validate.**
   Don't just trust the score blindly — glance at the seven votes:
   - Multiple `+1` from *trend* voters (EMA20/50, EMA200, SuperTrend) → trend trade.
   - Multiple `+1` from *momentum* voters (MACD, RSI, Vol+Break) → breakout trade.
   - One lone `−1` from RSI on a STRONG BUY usually means "trend is strong but
     overbought; entries on pullbacks are safer than chasing."

6. **Read the Entry / Stop / Target row.**
   - **Entry** is what *you* would pay (or short at) at the moment of the scan.
     It's not a limit order; it's a snapshot.
   - **Stop** is where you exit if you're wrong, set 2 × ATR away from entry.
     **Set this in your broker before walking away from the screen.**
   - **Target** is a 1:3 reward objective. Booking partially earlier (e.g.
     half at 1:1, trail the rest) is also reasonable — see Chandelier in §10.

7. **Position sizing (the rule that keeps you alive).**
   Decide first how many rupees you're willing to lose on this trade — call
   it `R` (e.g. 1% of capital). Then:

   ```
   shares = floor( R / |entry − stop| )
   ```

   Example: capital ₹1,00,000, risk 1% → R = ₹1,000. BHEL entry 404.60, stop
   375.62 → per-share risk = ₹28.98 → shares = 34. **Never** size based on
   "how much I can afford to buy"; always on "how much I can afford to lose."

8. **Click ▶ Live** if you want the page to keep itself current.
   The button turns red and pulses while live; pick the cadence (60s – 1h)
   from the selector next to it. For daily candles, a 5-minute or 15-minute
   refresh is plenty — the daily candle only closes once per session.

   **Live mode also tracks positions.** The first time a stock returns an
   actionable signal during a live session, the scanner *freezes* its
   entry / stop / target on the "Tracking" panel above the summary table.
   On every subsequent live scan it compares the latest price to those
   frozen levels:

   - long: stop hit if price ≤ stop, target hit if price ≥ target
   - short: stop hit if price ≥ stop, target hit if price ≤ target

   When either is hit, a macOS Notification Center alert fires (via the
   same `notify.py` the watcher uses) and the row in the Tracking panel
   gets a 🛑 / 🎯 badge. Tracked positions persist in `localStorage`, so a
   browser refresh doesn't lose them. Use the **Clear** button on the
   Tracking panel to reset.

9. **What to ignore.** Score ≥ +2 with the entry suppressed (ADX gate) is
   the analyzer telling you "the indicators agree but the market is too
   choppy to act". Wait for ADX to rise above 20 before acting on those
   signals — they're a watchlist hint, not a trade trigger.

### 4.4 Common newbie mistakes this tool can't save you from

- **Trading without a stop.** The Stop number is meaningless if you don't put
  the order in.
- **Moving the stop deeper "to give it room."** That converts a small loss
  into a ruinous one. Decide once, then leave it alone.
- **Stacking trades.** If three different stocks all flash BUY at 09:35, you
  are still allowed exactly one position. Pick the highest score, the most
  liquid name, or pass.
- **Trading illiquid scrips on signal alone.** CUPID, for example, is on
  NSE's **LTASM Stage IV surveillance** — circuit filters and tighter price
  bands apply. Always check `https://www.nseindia.com/get-quotes/equity?symbol=…`
  before placing a real order.
- **Equating "BUY" with "this will go up."** It means *the indicators currently
  agree the bias is up.* They are wrong all the time. Hence stops.

---

## 5. CLI cheatsheet

```bash
# scan whatever's in watchlist.txt (daily candles, 1y history)
python analyze.py

# scan ad-hoc symbols
python analyze.py CUPID DIVISLAB IRCTC

# only show actionable longs / shorts
python analyze.py --only buy
python analyze.py --only sell

# weekly candles for longer-term swings
python analyze.py --interval 1wk --period 2y

# shorter lookback if you only care about recent context
python analyze.py --period 3mo
```

CLI and Web UI run the *same* `analyze.analyze()` function — same numbers,
different rendering.

---

## 6. Desktop notifications (macOS) and the watcher

`watch.py` is a separate entry point that runs the scanner on a loop and
fires a macOS Notification Center alert whenever a stock's bucket changes.
Use it to leave the scanner running in the background while you do other
things and only get poked when the picture meaningfully shifts.

### 6.1 How it decides when to notify

Each scan produces a label per symbol:

```
STRONG SELL → SELL → HOLD/WAIT → BUY → STRONG BUY
```

The watcher remembers the previous label per symbol in `.watch_state.json`
and notifies when:

- the bucket changes (e.g. `HOLD → BUY`, `BUY → STRONG BUY`,
  `STRONG BUY → SELL` — the latter is a flip you definitely want to know
  about), and
- the new bucket is non-`HOLD` (default; toggle with `--include-hold`).

This naturally rate-limits: a stock that sits at `BUY` for an hour pings
you exactly once.

### 6.2 Running it

```bash
source .venv/bin/activate

python watch.py                       # watchlist.txt, 30-min polling, mac notifs
python watch.py CUPID RELIANCE TCS    # ad-hoc symbols
python watch.py --every 300           # poll every 5 minutes
python watch.py --once                # single pass and exit (good for cron)
python watch.py --all                 # every actionable scan, not just transitions
python watch.py --no-notify           # console-only (sanity check)
python watch.py --reset-state         # forget previous labels and start fresh
```

A typical alert lands as:

```
↑ RELIANCE — STRONG BUY
₹1436.00  ·  score +5
Entry 1436.0  ·  Stop 1418.0  ·  Target 1472.0
long swing  ·  ADX 32  ·  2×ATR stop  ·  1:3 R:R
```

Tip: since we're on daily candles, a single end-of-day scan is usually
enough. Set `--every 1800` (30 min) if you want intra-session updates as
the daily candle is still forming, or use a `cron` `--once` schedule.

### 6.3 Optional: nicer notifications

The default path uses the built-in `osascript`. Installing the optional
`terminal-notifier` gives you subtitles, custom sounds, and click-to-focus:

```bash
brew install terminal-notifier
```

`notify.py` auto-detects it.

### 6.4 Extending to Telegram / Slack / anything else

`notify.py` defines a one-method `Notifier` protocol:

```python
class Notifier(Protocol):
    name: str
    def send(self, title: str, message: str, subtitle: str | None = None) -> None: ...
```

A working stub `TelegramNotifier` is included; fill in the bot token and
chat id (or `export TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` and use
`TelegramNotifier.from_env()`), then add it to the watcher's notifier list:

```python
from notify import default_notifiers, TelegramNotifier
notifiers = default_notifiers() + [TelegramNotifier.from_env()]
```

The Telegram stub uses only `urllib`, so no extra pip dependency is needed.

### 6.5 Keep it running while you sleep (or work)

Daily candles only close once per day, so polling more than ~once per
30 minutes is wasted effort. Two reasonable setups:

**A) Single end-of-day cron (recommended).** Run a one-shot watcher
after market close to capture today's settled signals:

```cron
# 16:00 IST on weekdays, after NSE close — assumes system clock is IST
0 16 * * 1-5  cd ~/code/stocks && .venv/bin/python watch.py --once >> watch.log 2>&1
```

**B) Mid-session refresh.** If you also want intra-session pings on
strong moves (the daily bar is still forming, but ADX / Supertrend
direction may flip), run every 30 min during market hours:

```cron
*/30 9-15 * * 1-5  cd ~/code/stocks && .venv/bin/python watch.py --once >> watch.log 2>&1
```

A cleaner option on macOS is a `launchd` agent (`~/Library/LaunchAgents/
com.kaien.stocks.watch.plist`) so the OS keeps the process alive across
logins. Either way, `.watch_state.json` persists between invocations so
you don't get re-pinged for the same label.

> **Don't double-poll.** If you're also using the browser's ▶ Live mode,
> turn off the cron (or vice-versa). Two pollers on the same watchlist
> won't make signals appear faster — they'll just spend twice the
> API quota.

---

## 7. Backtesting (the reality check)

Before you ever consider auto-executing this strategy, run `backtest.py`.
It replays the same `analyze()` logic across 2 years of daily candles,
opens a virtual position on every actionable signal (vote ≥ 2 AND
ADX ≥ 20), exits on stop / target / max-hold timeout, and reports
results in **R-multiples** (where 1R is your planned per-trade risk)
so position size doesn't muddy the picture.

```bash
python backtest.py                              # 10 random NSE tickers, 2y daily
python backtest.py --count 20 --seed 42         # reproducible 20-symbol run
python backtest.py RELIANCE TCS INFY            # explicit symbols
python backtest.py --min-score 3                # only the strongest signals
python backtest.py --slippage-bps 0 --fees-pct 0   # isolate "strategy edge" from friction
python backtest.py --csv trades.csv             # dump per-trade CSV for analysis

# the recommended robust config — see §10 for the lift it delivers
python backtest.py --count 15 --seed 42 --longs-only --trailing chandelier
```

### What the metrics mean

| Metric | Meaning | Profitable strategy needs… |
| --- | --- | --- |
| Win rate | Trades that closed above entry | (depends on R:R) |
| Avg R | Average R-multiple per trade (net of costs) | **> 0** |
| Profit factor | Σ wins / |Σ losses| | **> 1.0**, ideally > 1.3 |
| Max DD (R) | Worst peak-to-trough on the equity curve | reasonable relative to total R |
| Hits T/S/Timeout | Targets / Stops / Max-hold flat-outs | high T, low S, low Timeout = clean |

### What this strategy actually scores

See §10.2 for the full A/B table. The short version on a 15-symbol
random sample, two seeds (42 = used during development, 7 = held-out):

| Variant | Trades | Win % | Avg R | Profit factor |
| --- | ---: | ---: | ---: | ---: |
| Baseline (no flags, seed 42) | 254 | 31.5 % | −0.027 | 0.96 |
| **`--longs-only --trailing chandelier`** (seed 42) | 881 | 69.4 % | +0.359 | 34.6 |
| **`--longs-only --trailing chandelier`** (seed 7, held-out) | 956 | 70.7 % | +0.285 | 29.5 |

Baseline is approximately break-even; with the robust flags the edge
is real on both seeds. **Do NOT** read the headline PF of 30+ as your
expected live performance — at 5× realistic friction it sits closer to
PF 4 (see §10.3 for the stress-test).

### What the backtester does (and does NOT) model

Does model:
- Walk-forward, no look-ahead (each decision sees only `df.iloc[:t+1]`)
- Execution at the *next* bar's open (realistic "saw signal at close,
  ordered immediately" timing)
- Slippage on entry and exit (bps of price, configurable)
- Round-trip fees as % of notional (≈ broker + STT)
- ADX(14) gate: trades are only opened when ADX ≥ 20
- Chandelier trailing exit when `--trailing chandelier` is set;
  R-multiples still denominated by the ORIGINAL 2×ATR stop so trailing
  doesn't inflate R
- Max-hold timeout: positions force-flat after 40 bars (~ 2 trading months)
- Conservative same-bar handling: if both stop and target are inside a
  single bar's range, assume **stop fills first**
- Debouncing: don't re-open the same direction immediately after a stop

Does NOT model:
- **Overnight gaps through the stop.** If a stock gaps DOWN through
  your stop, the backtest fills you at the stop level; in reality you
  fill at the gap-open price (which can be much worse).
- Corporate actions (splits, bonuses, dividends as cash adjustments)
- Real-time order routing failures, partial fills, rejects
- Liquidity holes on smallcaps (the universe is liquid largecaps + midcaps)
- Position sizing / Kelly fraction — we report per-trade R, you choose size
- Bracket / SL-M order behaviour
- Corporate actions, dividends, splits
- Liquidity constraints on actually getting filled at the size you'd
  need for ₹1,000 risk / trade

These all push the real-world result **below** the backtest result, not
above it. If the backtest is losing, live trading will lose faster.

---

## 8. Extending the tool (ideas, not promises)

- **Sparkline per card** — last 30 closes drawn as a tiny SVG inside each card.
- **More notifier channels** — Slack, ntfy.sh, email (the `Notifier`
  protocol is already there; one class per channel).
- **Walk-forward parameter tuning** — split history into train/test
  windows and let `min_score`, `chandelier_mult`, and `adx_floor` move
  on the train side; cement defaults from there.
- **Pyramiding** — add a 2nd unit when price moves +1R from entry
  (classic Turtle). Doubles avg win on real trends.
- **Volatility-scaled position sizing** — invert ATR% to size positions
  so equal risk per trade actually means equal portfolio risk.
- **Index regime filter** — fetch NIFTY in parallel and only take longs
  when index > 200-day EMA. Sits behind a one-line flag.
- **Risk presets** — store capital + per-trade risk %, auto-compute share
  count beside every signal in the UI.

---

## 9. The screener and the auto-updating watchlist

### 9.1 The screener (`screener.py`)

A separate entry point that *picks* the symbols the rest of the tooling
operates on. It fetches 6 months of daily candles for every name in
`universe.txt`, scores each one on three axes, and writes the top N to
`watchlist.txt`.

Composite score (each component z-scored across the universe):

```
score = 0.55 * momentum_z + 0.25 * liquidity_z + 0.20 * jump_z
```

- **Momentum** = average of (1-month return + 3-month return +
  (-distance-from-52w-high)). High when a stock is trending up and near
  its 52-week high.
- **Liquidity** = `log10(avg daily turnover in ₹ Cr over last 20 days)`.
  Log-scaled because turnover spans 2-3 orders of magnitude.
- **Jumpiness** = ATR%/price + largest 5-day return in last 20 sessions.
  Captures "stock that moves" — useful so your stops have room to
  breathe without instant whipsaws.

Filters before ranking:

- Price ≥ ₹50 (no pennies)
- Avg daily turnover ≥ ₹5 Cr (real liquidity)
- ≥ 80 bars of history (no recent listings)
- ≤ 40% below 52-week high (no broken stocks)

Usage:

```bash
python screener.py                          # default: top 15, side=long
python screener.py --top 25
python screener.py --side short             # rank by downtrend strength
python screener.py --w-momentum 0.7 --w-jump 0.3
python screener.py --universe my_list.txt --output momentum_picks.txt
python screener.py --dry-run                # show ranking, don't write
```

Output looks like:

```
ADANIPOWER   # rank  1  score +3.42  1m +37.9%  3m +68.3%  ₹1232Cr  ATR 3.4%
BHEL         # rank  2  score +3.42  1m +59.5%  3m +55.3%  ₹911Cr   ATR 3.6%
ADANIGREEN   # rank  3  score +2.77  1m +46.5%  3m +57.9%  ₹584Cr   ATR 3.3%
...
```

### 9.2 Recommended cadence

Run the screener once at end-of-day, then let the rest of the tooling
inherit the new picks:

```cron
# 16:00 IST weekdays — refresh watchlist after NSE close
0 16 * * 1-5  cd ~/code/stocks && .venv/bin/python screener.py --top 20 >> screener.log 2>&1
```

After it runs, `watchlist.txt` has the freshest 20 high-momentum names.
`python analyze.py` (next morning), `python watch.py`, and the web UI
all see them automatically.

### 9.3 Counter-intuitive note on the backtest result

When you backtest the strategy on a **random** NSE sample, the baseline
(no robust flags) scores PF ≈ 0.94. When you backtest it on the
**screener's own picks** (already-rising momentum stocks), it scores
worse — around PF ≈ 0.77.

Why? The screener picks stocks that have already moved 30–60 % in three
months. The RSI and Bollinger voters then tag those as overextended and
try to short them — or fire late-cycle buys that get caught at the top
of a pullback. The screener and the entry rules have to be
philosophically aligned.

Two ways to reconcile:

1. Use the screener for the **universe**, then let the voters decide
   *when* to enter / exit (rather than acting on every signal).
2. Switch the screener to rank for **trend persistence + pullback**
   rather than raw momentum (e.g. require RSI < 60 even if the stock
   is in a 1-year uptrend) — that's the classic "buy the dip in strong
   stocks" recipe.

The robust config (§10) helps here too: the ADX gate kills entries on
choppy late-cycle stocks, and the Chandelier exit cuts losing trades
quickly instead of waiting for a fixed stop.

---

## 10. The robustness pass — popular algos that actually moved the needle

The earlier swing version (six voters, fixed 2×ATR stop, 1:3 R:R target)
got us to PF ≈ 0.94 — close to break-even but not profitable. So this
pass added four pieces that every trend-following book recommends, and
A/B tested each one. Code is in `analyze.py` (indicators + voter + gate)
and `backtest.py` (trailing + longs-only flag).

### 10.1 What was added

| # | Technique | Inventor / origin | Where it lives |
| - | --------- | ----------------- | -------------- |
| 1 | **ADX(14) trend-strength gate** | J. Welles Wilder, 1978 | `analyze.compute_indicators` + ADX-floor check inside `analyze_df` |
| 2 | **Supertrend(10, 3.0) voter** | Olivier Seban — most-used indicator in Indian retail | `analyze.supertrend` + voter #7 in `analyze_df` |
| 3 | **Chandelier trailing exit** | Chuck LeBeau — `stop = max(high since entry) − 3×ATR` | `backtest.backtest_symbol` (`--trailing chandelier`) |
| 4 | **Longs-only mode** | Empirical: Indian equities are structurally long-biased | `--longs-only` flag in `backtest.py` |

How they interact:

- **ADX gate.** Below ADX 20 the market is sideways; trend-following
  loses there. The analyzer still emits a label (so the UI shows the
  signal) but it sets `entry/stop/target = None`, so neither the live
  UI nor the backtester opens a position. This single change cleans
  out roughly a third of the noisy signals.
- **Supertrend voter** raises the voter count to 7 — `classify()` already
  scales the STRONG threshold to `voters − 1`, so no other code had to
  move.
- **Chandelier exit.** Replaces the fixed 1:3 R:R target with a trailing
  stop set 3 × ATR below the highest high seen since entry (mirrored
  for shorts). The stop only ratchets up, never down. Lets winners run
  past 3R when a real trend extends; cuts winners short who fail to
  follow through.
- **Longs-only** simply skips short signals at entry time.

### 10.2 A/B test results (two seeds, ~15-symbol random universe each, 2y daily)

All numbers are net of 5 bps/side slippage + 0.05 % round-trip fees.

**Seed 42** (the one we tuned earlier):

| Variant | Trades | Win % | Avg R | PF | Total R |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline swing (no flags) | 254 | 31.5 % | −0.027 | 0.96 | −6.8 |
| `--longs-only` only | 156 | 32.7 % | +0.067 | 1.10 | +10.5 |
| `--trailing chandelier` only | 1,693 | 69.5 % | +0.340 | 34.5 | +575.0 |
| **`--longs-only --trailing chandelier`** | **881** | **69.4 %** | **+0.359** | **34.6** | **+316.6** |

**Seed 7** (held-out — never used during development):

| Variant | Trades | Win % | Avg R | PF | Total R |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline swing | 260 | 30.8 % | −0.106 | 0.85 | −27.6 |
| **`--longs-only --trailing chandelier`** | **956** | **70.7 %** | **+0.285** | **29.5** | **+272.7** |

The robust configuration replicates on the held-out seed (same shape,
similar magnitude), so this isn't a curve-fit on seed 42.

### 10.3 Stress-test (don't trust the headline PF)

A profit factor north of 30 is a giant red flag in a real trading
strategy — published trend-following systems sit in the PF 1.5 – 2.5
range. Two things drive the inflation:

1. **The Chandelier exit produces many micro-trades.** Once a trend
   extends, the trail prints, we exit, then re-enter on the next bar.
   Each "win" is small (avg +0.53R) but losses are tiny (avg −0.03R)
   because most exits happen near-entry once the trail has tightened.
   With 880-1700 trades the ratio looks spectacular.
2. **Backtest costs are linear in trade count.** Real slippage
   on re-entries during fast moves is usually worse than 5 bps. To
   stress this we re-ran the robust config with 5× costs (25 bps/side
   slippage + 0.20 % fees):

| Cost regime | Trades | Win % | Avg R | PF |
| --- | ---: | ---: | ---: | ---: |
| 5 bps/side + 0.05 % (default) | 881 | 69.4 % | +0.359 | 34.6 |
| 10 bps/side + 0.10 % (2×) | 881 | n/a | +0.319 | 15.0 |
| 25 bps/side + 0.20 % (5× / catastrophic) | 881 | n/a | +0.211 | 4.3 |

Even at 5× realistic friction the strategy still nets +0.21R per trade.
So the trailing-exit upgrade is **real**, but expect live PF in the
**3–6 range, not 30**, after you account for slippage on re-entry,
overnight gaps (the backtester doesn't model them — gaps DOWN through
the trail would land you below the modelled exit), and the occasional
liquidity hole on a small-cap.

### 10.4 What this means in practice

- Run `python backtest.py --longs-only --trailing chandelier`
  before believing a paper-trade result; it's the recommended default
  config for any new strategy variation.
- The web UI / watcher don't apply Chandelier (it's an exit method, not
  a signal). They still emit ADX-gated entries from the 7-voter
  consensus, which is the right starting point. The Chandelier is what
  you would do AFTER entering: trail the stop yourself, don't book at
  1:3 R:R.
- Don't conclude "free money". Paper-trade for at least one full month
  with the recommended config and compare your live PF to the
  stressed-cost row above. If you see PF < 2 live, the strategy is
  marginal; if PF > 3 live, the edge is plausibly real on Indian
  equities during 2024-2026's regime.

### 10.5 What was NOT added (and why)

A few popular techniques were considered and dropped:

- **Weekly higher-timeframe filter (Elder Triple-Screen).** Already
  approximated by the EMA200 voter on daily candles, which captures the
  same long-term trend stage. Adding a weekly resample would double the
  data pulls and shouldn't change much.
- **Index regime filter (only-long-when-NIFTY > 200dMA).** Useful but
  requires a second yfinance fetch on every scan and the back-test
  already shows long-biased equities ride the index trend; the EMA200
  voter does most of this job for individual names.
- **Williams %R / Stochastic.** These overlap heavily with the existing
  RSI(14) voter; adding them gives no new information.
- **Donchian 20-day breakout as an extra voter.** Already implicitly
  covered by the Vol+Break voter, which fires on 20-day high/low + 1.3×
  volume.
- **Kelly-fraction position sizing.** Out of scope for a tool that
  emits R-multiples; you can size your own positions from the entry,
  stop, and a fixed ₹ risk per trade.

If you want to actually beat the current numbers, the things worth
trying next (in roughly descending order of expected lift) are:

1. **Walk-forward parameter tuning** — split the data into train / test
   chunks and let `min_score`, `chandelier_mult`, and `adx_floor` move
   on the train side. Right now we use defaults.
2. **Volatility scaling at the position level** — bigger size when ATR%
   is low, smaller when ATR% is high. Doesn't change PF but cuts
   drawdown.
3. **Pyramid into winners** — add a 2nd unit when price is +1R from
   entry. Standard Turtle move. Roughly doubles avg win in trends.

---

## 11. File map

```
stocks/
├── analyze.py            # signal engine: 7 swing voters (incl. Supertrend) + ADX(14) gate
├── app.py                # Flask web server (Scanner + /api/scan + /api/notify + /docs)
├── backtest.py           # walk-forward backtester (--longs-only, --trailing chandelier)
├── screener.py           # universe ranker → rewrites watchlist.txt
├── notify.py             # Mac / Console / Telegram-stub notifiers
├── watch.py              # long-running watcher → desktop notifications on transitions
├── templates/
│   ├── index.html        # web UI: Mode selector, Live mode, stop/target alerts, Tracking panel
│   └── docs.html         # /docs renderer for documentation.md
├── universe.txt          # ~150 NSE candidates that the screener ranks
├── watchlist.txt         # auto-generated by screener; consumed by everything else
├── requirements.txt      # pip deps
├── .watch_state.json     # auto-generated; remembers last label per symbol
├── README.md             # short setup notes
└── documentation.md      # ← this file (HLD + beginner guide)
```
