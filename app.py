"""Tiny Flask front-end for the swing signal scanner.

Reuses analyze.analyze() and exposes:
  GET  /                   → single-page UI
  GET  /api/watchlists     → momentum picks metadata + symbols
  POST /api/rescreen       → re-run screener and refresh momentum picks
  POST /api/scan           → JSON {symbols:[], interval, period} → list[Analysis]
  GET  /api/portfolio      → Groww holdings joined with live LTP + P&L
  POST /api/notify         → bridge browser stop/target alerts to macOS Notification Center
  GET  /docs               → renders documentation.md as HTML
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import markdown as md
from flask import Flask, abort, jsonify, render_template, request
from markupsafe import Markup

import groww_client  # for /api/portfolio
from analyze import SCRIPT_DIR, analyze, load_watchlist
from notify import default_notifiers

# Single watchlist used by the web app: auto-screened momentum picks.
MOMENTUM_PATH = SCRIPT_DIR / "momentum_picks.txt"


def _read_watchlist_meta(path: Path) -> dict:
    """Parse a watchlist file → {symbols, meta:{generated, header_lines, raw}}.

    Header comments (lines starting with '#' at the top) are preserved so the
    UI can display screener metadata like 'top 15 of 148, generated 01:05 IST'.
    """
    if not path.exists():
        return {"symbols": [], "meta": {"header": "", "exists": False, "path": path.name}}
    raw = path.read_text(encoding="utf-8")
    header_lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("#"):
            header_lines.append(line.lstrip("# ").rstrip())
        elif line.strip() == "":
            if header_lines:  # blank line after comments → end of header block
                break
        else:
            break
    return {
        "symbols": load_watchlist(path),
        "meta": {
            "header": "\n".join(header_lines),
            "exists": True,
            "path": path.name,
            "mtime": path.stat().st_mtime,
        },
    }

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
NOTIFIERS = default_notifiers()

MD_EXTENSIONS = [
    "fenced_code",   # ```code``` blocks
    "tables",        # GFM-style tables
    "toc",           # heading anchors + optional [TOC]
    "sane_lists",    # don't merge unrelated lists
    "codehilite",    # syntax highlighting via Pygments
]
MD_CONFIG = {
    "toc": {"permalink": "¶", "toc_depth": "2-3"},
    "codehilite": {"guess_lang": False, "css_class": "codehilite"},
}


@app.get("/")
def index():
    # Watchlist contents are fetched client-side via /api/watchlists.
    return render_template("index.html")


@app.get("/api/watchlists")
def api_watchlists():
    return jsonify({
        "momentum": _read_watchlist_meta(MOMENTUM_PATH),
    })


@app.get("/docs")
def docs():
    path = SCRIPT_DIR / "documentation.md"
    if not path.exists():
        abort(404, description="documentation.md not found")
    converter = md.Markdown(extensions=MD_EXTENSIONS, extension_configs=MD_CONFIG)
    html = converter.convert(path.read_text(encoding="utf-8"))
    toc = getattr(converter, "toc", "")
    return render_template("docs.html", content=Markup(html), toc=Markup(toc))


@app.post("/api/rescreen")
def api_rescreen():
    """Re-run screener.py to refresh momentum_picks.txt, return the new list.

    Synchronous: takes ~30-60 s because the screener fetches every symbol in
    universe.txt one-by-one through the Groww API. The UI shows a spinner.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "screener.py")],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "screener timed out (>180 s)"}), 504
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "screener.py failed").strip()
        return jsonify({"error": msg[-500:]}), 500
    return jsonify(_read_watchlist_meta(MOMENTUM_PATH))


@app.post("/api/scan")
def scan():
    body = request.get_json(force=True) or {}
    symbols = [s.strip().upper() for s in body.get("symbols", []) if s.strip()]
    interval = body.get("interval", "1d")
    period = body.get("period", "1y")

    out: list[dict] = []
    for s in symbols:
        try:
            a = analyze(s, period=period, interval=interval)
        except Exception as e:
            out.append({"symbol": s, "error": f"{type(e).__name__}: {e}"})
            continue
        if a is None:
            out.append({"symbol": s, "error": "no data"})
            continue
        d = asdict(a)
        d["last_time"] = a.last_time.isoformat()
        out.append(d)

    out.sort(key=lambda x: -(x.get("total") or 0))
    return jsonify(out)


@app.get("/api/portfolio")
def portfolio():
    """Return the authenticated user's Groww holdings, joined with live LTP
    and unrealized-P&L. Pure read; never places orders."""
    try:
        rows = groww_client.get_portfolio()
        total_invested = sum((r["invested"] or 0) for r in rows)

        # Only rows that actually got a live price contribute to current value
        # and P&L. If the "Live Data" subscription is off, every ltp is None →
        # we must NOT report current_value 0 / pnl -100% (that's misleading).
        priced = [r for r in rows if r.get("ltp") is not None]
        live_data = len(priced) > 0

        if live_data:
            priced_invested = sum(r["invested"] for r in priced)
            total_value = sum(r["current_value"] for r in priced)
            total_pnl = total_value - priced_invested
            totals = {
                "invested": round(total_invested, 2),
                "current_value": round(total_value, 2),
                "pnl_abs": round(total_pnl, 2),
                "pnl_pct": round(total_pnl / priced_invested * 100, 2) if priced_invested else 0.0,
                "count": len(rows),
                "priced_count": len(priced),
            }
        else:
            totals = {
                "invested": round(total_invested, 2),
                "current_value": None,
                "pnl_abs": None,
                "pnl_pct": None,
                "count": len(rows),
                "priced_count": 0,
            }

        return jsonify({
            "rows": rows,
            "totals": totals,
            "live_data": live_data,
            "quote_error": groww_client.last_quote_error(),
        })
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.post("/api/notify")
def api_notify():
    """Bridge browser-side level-crossing detection to native notifiers.

    Body: {title: str, message: str, subtitle?: str}
    The browser can't reach macOS Notification Center directly, so the live
    scanner POSTs here when a tracked stop or target is hit and we fan the
    alert out to whatever notifiers are configured (MacNotifier by default).
    """
    body = request.get_json(force=True) or {}
    title = (body.get("title") or "stocks").strip()
    message = (body.get("message") or "").strip()
    subtitle = body.get("subtitle")
    if subtitle is not None:
        subtitle = str(subtitle).strip() or None
    sent = []
    for n in NOTIFIERS:
        try:
            n.send(title, message, subtitle=subtitle)
            sent.append(n.name)
        except Exception as e:  # noqa: BLE001
            sent.append(f"{n.name}:error({type(e).__name__})")
    return jsonify({"ok": True, "sent": sent})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5051, debug=False)
