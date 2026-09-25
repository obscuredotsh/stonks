"""Pluggable notifier abstractions for the stocks scanner.

A `Notifier` is anything with a `.send(title, message, subtitle=None)` method.
The watcher fans out each alert to every notifier in its list, so adding a new
channel (Slack, Telegram, email, ntfy.sh ...) is a matter of dropping a class
in here and appending an instance to the list in `watch.py`.

Default channel on macOS is the system Notification Center, reached via
`osascript`. If the optional `terminal-notifier` binary is on $PATH it is used
instead because it gives nicer formatting (subtitle, icon, click-to-focus).

Install (optional, nicer notifications):
    brew install terminal-notifier
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol


class Notifier(Protocol):
    """Anything that can deliver a (title, subtitle, message) triple."""

    name: str

    def send(self, title: str, message: str, subtitle: str | None = None) -> None: ...


def _esc_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class MacNotifier:
    """macOS Notification Center.

    Uses `terminal-notifier` if available (richer); falls back to the
    built-in `osascript display notification`. Both are no-extra-Python-deps.
    """

    name: str = "macos"
    sound: str | None = "Glass"  # any name from /System/Library/Sounds, or None for silent

    def send(self, title: str, message: str, subtitle: str | None = None) -> None:
        if sys.platform != "darwin":
            ConsoleNotifier().send(title, message, subtitle)
            return
        if shutil.which("terminal-notifier"):
            self._send_terminal_notifier(title, message, subtitle)
        else:
            self._send_osascript(title, message, subtitle)

    def _send_terminal_notifier(self, title: str, message: str, subtitle: str | None) -> None:
        cmd = ["terminal-notifier", "-title", title, "-message", message, "-group", "stocks"]
        if subtitle:
            cmd += ["-subtitle", subtitle]
        if self.sound:
            cmd += ["-sound", self.sound]
        subprocess.run(cmd, check=False, capture_output=True)

    def _send_osascript(self, title: str, message: str, subtitle: str | None) -> None:
        script = (
            f'display notification "{_esc_applescript(message)}" '
            f'with title "{_esc_applescript(title)}"'
        )
        if subtitle:
            script += f' subtitle "{_esc_applescript(subtitle)}"'
        if self.sound:
            script += f' sound name "{_esc_applescript(self.sound)}"'
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


@dataclass
class ConsoleNotifier:
    """Prints to stdout. Used as a fallback on non-mac systems and for tests."""

    name: str = "console"

    def send(self, title: str, message: str, subtitle: str | None = None) -> None:
        line = f"[{title}]"
        if subtitle:
            line += f"  {subtitle}"
        line += f"  — {message}"
        print(line, flush=True)


@dataclass
class TelegramNotifier:
    """Stub Telegram notifier — fill in `token` and `chat_id` to enable.

    Reads credentials from env vars by default:
        TELEGRAM_BOT_TOKEN   bot token from @BotFather
        TELEGRAM_CHAT_ID     numeric chat id (yours, a group, or a channel)

    Usage:
        from notify import TelegramNotifier
        notifiers.append(TelegramNotifier.from_env())

    This deliberately uses only stdlib `urllib` so there's no extra pip dep.
    """

    name: str = "telegram"
    token: str = ""
    chat_id: str = ""

    @classmethod
    def from_env(cls) -> "TelegramNotifier":
        return cls(
            token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        )

    def send(self, title: str, message: str, subtitle: str | None = None) -> None:
        if not self.token or not self.chat_id:
            return  # silently skip if not configured
        text_lines = [f"*{title}*"]
        if subtitle:
            text_lines.append(f"_{subtitle}_")
        text_lines.append(message)
        text = "\n".join(text_lines)
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}
        ).encode("utf-8")
        try:
            with urllib.request.urlopen(url, data=data, timeout=5) as resp:
                resp.read()
        except Exception as e:
            print(f"[telegram notifier failed]: {e}", file=sys.stderr)


def default_notifiers() -> list[Notifier]:
    """Pick a sensible default for this machine."""
    if sys.platform == "darwin":
        return [MacNotifier()]
    return [ConsoleNotifier()]


if __name__ == "__main__":
    # `python notify.py "title" "message"` to test the desktop notification.
    title = sys.argv[1] if len(sys.argv) > 1 else "stocks · test"
    message = sys.argv[2] if len(sys.argv) > 2 else "Hello from the stocks notifier."
    subtitle = sys.argv[3] if len(sys.argv) > 3 else None
    for n in default_notifiers():
        n.send(title, message, subtitle)
    print("sent.")
