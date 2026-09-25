#!/usr/bin/env bash
# Manage the stocks-groww Flask app.
#
#   ./runtime.sh start    – launch app.py in background if not already running
#   ./runtime.sh stop     – SIGTERM (then SIGKILL after 5s); also cleans up
#                           any orphan listener on port 5051
#   ./runtime.sh restart  – stop + start
#   ./runtime.sh status   – is it up, on what pid, listening on what port
#
# Flags (any order, after the command):
#   --open               – open the app in a browser (Opera by default)
#   --no-open            – never open a browser
#   --opera | --chrome | --safari | --default
#                        – which browser --open should use (default: Opera)
#   --market-hours-only  – no-op unless it's Mon–Fri, 09:14–15:35 IST. Used by
#                          the launchd agents so a wake/login outside the NSE
#                          window doesn't spin the app up.
#
# Designed to be called both interactively and by launchd. The LaunchAgents
# (com.kaien.stocks-groww.{start,stop}) drive the NSE window automatically.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/app.pid"
LOGFILE="$DIR/app.log"
PYTHON="$DIR/.venv/bin/python"
APP="$DIR/app.py"
PORT=5051

stamp() { date '+%F %T %Z'; }

is_alive() { [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null; }

# True (0) when now is within the NSE session window: Mon–Fri, 09:14–15:35 IST.
# The host runs on IST, so local time is fine (no TZ conversion needed).
within_market_hours() {
    local dow hm
    dow="$(date +%u)"    # 1=Mon … 7=Sun
    hm="$(date +%H%M)"   # e.g. 0914, 1535
    [[ "$dow" -ge 1 && "$dow" -le 5 ]] || return 1
    # 10# forces base-10 so a leading-zero time like 0914 isn't read as octal.
    (( 10#$hm >= 914 && 10#$hm <= 1535 ))
}

# Open the app URL in the chosen browser. Falls back to the system default if
# the requested app isn't installed.
open_in_browser() {
    local url="http://127.0.0.1:$PORT"
    local app="${BROWSER_APP:-Opera}"
    if [[ "$app" == "default" ]]; then
        open "$url" || true
        echo "[runtime $(stamp)] opened $url in default browser"
        return
    fi
    if open -a "$app" "$url" 2>/dev/null; then
        echo "[runtime $(stamp)] opened $url in $app"
    else
        echo "[runtime $(stamp)] '$app' not found — falling back to default browser"
        open "$url" || true
    fi
}

pid_from_file() {
    [[ -f "$PIDFILE" ]] || { echo ""; return; }
    cat "$PIDFILE"
}

pid_from_port() {
    # Whoever (if anyone) currently holds the listener on $PORT.
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

resolve_pid() {
    local pid
    pid="$(pid_from_file)"
    is_alive "$pid" && { echo "$pid"; return; }
    pid_from_port
}

cmd_start() {
    # Guard for launchd-driven wake/login starts: bail quietly outside the
    # NSE window so we don't open Opera at, say, 11pm on a Sunday.
    if [[ "${MARKET_HOURS_ONLY:-no}" == "yes" ]] && ! within_market_hours; then
        echo "[runtime $(stamp)] outside NSE hours (Mon-Fri 09:14-15:35 IST) — not starting"
        exit 0
    fi

    local pid first_run=1
    pid="$(resolve_pid)"
    if is_alive "$pid"; then
        echo "[runtime $(stamp)] already running (pid $pid) on :$PORT"
        echo "$pid" >"$PIDFILE"
        first_run=0
    else
        # 1 MB log rotation – keep one .old, drop everything before that.
        if [[ -f "$LOGFILE" ]] && [[ "$(wc -c <"$LOGFILE")" -gt 1048576 ]]; then
            mv -f "$LOGFILE" "$LOGFILE.old"
        fi
        {
            echo
            echo "===== [runtime $(stamp)] starting app.py ====="
        } >>"$LOGFILE"
        cd "$DIR"
        nohup "$PYTHON" "$APP" >>"$LOGFILE" 2>&1 &
        echo $! >"$PIDFILE"
        # Wait up to 5s for the port to actually start listening — opening
        # the browser before Flask binds gives the user a "can't connect"
        # error, not a great first impression.
        for _ in 1 2 3 4 5; do
            if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
                break
            fi
            sleep 1
        done
        pid="$(cat "$PIDFILE")"
        if is_alive "$pid"; then
            echo "[runtime $(stamp)] started, pid $pid, http://127.0.0.1:$PORT"
            echo "[runtime $(stamp)] log: $LOGFILE"
        else
            echo "[runtime $(stamp)] FAILED to start, tail of log:"
            tail -n 20 "$LOGFILE" || true
            exit 1
        fi
    fi

    # Open the browser when asked (--open), or automatically when invoked
    # interactively (stdout is a TTY). launchd redirects stdout to a file, so
    # 'auto' no-ops there — the start agent passes --open explicitly.
    if [[ "${OPEN_BROWSER:-auto}" == "yes" ]] \
       || { [[ "${OPEN_BROWSER:-auto}" == "auto" ]] && [[ -t 1 ]]; }; then
        open_in_browser
    fi
}

cmd_stop() {
    local pid
    pid="$(resolve_pid)"
    if ! is_alive "$pid"; then
        echo "[runtime $(stamp)] not running"
        rm -f "$PIDFILE"
        exit 0
    fi
    echo "[runtime $(stamp)] stopping pid $pid"
    kill "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
        is_alive "$pid" || break
        sleep 1
    done
    if is_alive "$pid"; then
        echo "[runtime $(stamp)] still alive, SIGKILL"
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
    echo "===== [runtime $(stamp)] stopped =====" >>"$LOGFILE"
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_status() {
    local pid
    pid="$(resolve_pid)"
    if is_alive "$pid"; then
        echo "[runtime $(stamp)] UP    pid $pid on http://127.0.0.1:$PORT"
        echo "  recent log:"
        tail -n 5 "$LOGFILE" 2>/dev/null | sed 's/^/    /'
    else
        echo "[runtime $(stamp)] DOWN  (no pid file, no listener on :$PORT)"
    fi
}

# First positional arg is the command; everything after is flags (any order).
CMD="${1:-}"
shift || true
for arg in "$@"; do
    case "$arg" in
        --open)              OPEN_BROWSER=yes ;;
        --no-open)           OPEN_BROWSER=no  ;;
        --opera)             BROWSER_APP=Opera ;;
        --chrome)            BROWSER_APP="Google Chrome" ;;
        --safari)            BROWSER_APP=Safari ;;
        --default)           BROWSER_APP=default ;;
        --market-hours-only) MARKET_HOURS_ONLY=yes ;;
        *) echo "[runtime] unknown flag: $arg" >&2 ;;
    esac
done

case "$CMD" in
    start)   cmd_start   ;;
    stop)    cmd_stop    ;;
    restart) cmd_restart ;;
    status)  cmd_status  ;;
    *) echo "usage: $0 {start|stop|restart|status} [--open|--no-open] [--opera|--chrome|--safari|--default] [--market-hours-only]" >&2; exit 2 ;;
esac
