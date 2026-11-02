"""Notifications: Discord embeds, or any Apprise URL as markdown (see notice.py)."""

from __future__ import annotations

import logging
import time

from .bus import push_alert
from .findings import Finding
from .notice import Message, Outbox, Section, digest
from .state import State
from .watch import Change

log = logging.getLogger(__name__)

AUTHOR = "Sensi agent"
MAX_UNSENT = 100  # changes kept for retry while notifications can't be delivered
LATE = 120  # a change delivered later than this says when it happened


def _detail(change: Change, now: float) -> str:
    if now - change.at > LATE:
        return f"{change.detail} · at {time.strftime('%H:%M', time.localtime(change.at))}"
    return change.detail


class Notifier:
    def __init__(self, urls: list[str], renotify_hours: int, state: State, events: list[str],
                 bot_url: str = "", bot_token: str = ""):
        self.renotify_hours = renotify_hours
        self.state = state
        self.events = set(events)
        self.bot_url, self.bot_token = bot_url, bot_token  # assistant first, Apprise as fallback
        self.outbox = Outbox(urls)
        self.unsent: list[Change] = []

    def changes(self, changes: list[Change]) -> None:
        """One message per thermostat. These go to the notification channel only: a furnace cycling
        all day isn't something for the assistant to look into. Undelivered ones are retried."""
        pending = self.unsent + [c for c in changes if c.kind in self.events]
        self.unsent = []
        now = time.time()
        by_thermostat: dict[str, list[Change]] = {}
        for c in pending:
            by_thermostat.setdefault(c.thermostat, []).append(c)
        for name, group in by_thermostat.items():
            if len(group) == 1:
                c = group[0]
                msg = Message(f"{c.icon} {name}: {c.headline}", "info", [Section("", [_detail(c, now)], bullets=False)], AUTHOR)
            else:
                lines = [f"{c.icon} {c.headline} ({_detail(c, now)})" for c in group]
                msg = Message(f"{group[-1].icon} {name}: {len(group)} changes", "info",
                              [Section("", lines, bullets=False)], AUTHOR)
            log.info("%s: %s", name, "; ".join(f"{c.headline} ({c.detail})" for c in group))
            if not self.outbox.send(msg):
                self.unsent += group
        del self.unsent[:-MAX_UNSENT]

    def problems(self, findings: list[Finding]) -> bool:
        """Offline thermostats and cloud trouble: new ones, reminders every renotify_hours, and cleared
        ones. Nothing counts as sent until it's delivered. True if anything was recorded."""
        notified = self.state["notified"]
        now = time.time()
        current = {f.key for f in findings}
        due = [f for f in findings if f.key not in notified or now - notified[f.key]["ts"] >= self.renotify_hours * 3600]
        gone = [k for k in notified if k not in current]
        if not due and not gone:
            return False
        msg = digest(AUTHOR, due, [notified[k]["summary"] for k in gone])
        body = msg.markdown()
        log.info("%s\n%s", msg.title, body)
        if not (self.bot_url and push_alert(self.bot_url, self.bot_token, "sensi", msg.title, body, due, None)) \
                and not self.outbox.send(msg):
            return False
        for k in gone:
            del notified[k]
        for f in due:
            notified[f.key] = {"ts": now, "summary": f.summary, "severity": f.severity}
        return True
