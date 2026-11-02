"""Entry point: python -m sensi_agent"""

from __future__ import annotations

import logging
import os
import queue
import time

from . import health
from .bus import Approvals, BusTool, Runtime, schema, serve_mcp
from .config import Config
from .findings import Finding
from .notify import Notifier
from .sensi import AuthRejected, Feed, Tokens
from .state import State
from .watch import Watcher

log = logging.getLogger("sensi_agent")

TICK = 5  # seconds between looks at the feed
MAX_BACKOFF = 300
LOGIN_ADVICE = "sign in at manager.sensicomfort.com and set a new SENSI_REFRESH_TOKEN (see README)"


def bus_tools(runtime: Runtime, watcher: Watcher, state: State) -> list[BusTool]:
    """What the assistant may call: all read-only. Nothing in this agent can change a thermostat."""

    def status() -> dict:
        return {"thermostats": watcher.summary(), "open_alerts": [v["summary"] for v in state["notified"].values()]}

    def history(limit: int = 50, thermostat: str = "") -> dict:
        changes = state["history"].get("changes", [])
        if thermostat:
            changes = [c for c in changes if c["thermostat"].lower() == thermostat.lower()]
        return {"changes": changes[-max(1, min(int(limit), 500)):]}

    return runtime.common_tools(check_is_destructive=False) + [
        BusTool("sensi_status", "Sensi: each thermostat's room temperature, humidity, mode, set temperatures, "
                "and whether the heat or AC is running.", schema(), status),
        BusTool("sensi_history", "Sensi: recent changes, oldest first: heat and AC turning on and off, set "
                "temperature changes and mode changes, with Unix timestamps.",
                schema({"limit": {"type": "integer", "description": "How many, at most 500 (default 50)"},
                        "thermostat": {"type": "string", "description": "Only this thermostat, by name"}}), history),
    ]


class Agent:
    def __init__(self, cfg: Config, feed: Feed, watcher: Watcher, notifier: Notifier, state: State, runtime: Runtime):
        self.cfg, self.feed, self.watcher, self.notifier, self.state = cfg, feed, watcher, notifier, state
        self.runtime = runtime
        self.connected, self.login_bad = False, False
        self.backoff, self.last_try = TICK, 0.0
        self.last_data = self.quiet_since = time.time()

    def _connection(self, now: float) -> None:
        if self.connected and self.feed.closed.is_set():
            log.info("Connection to Sensi closed")
            self._drop()
        elif self.connected and now - self.quiet_since >= self.cfg.resync_seconds:
            # Quiet is normal for a push feed, but a connection can also go stale without closing,
            # and a new connection always starts with the full state.
            self._drop()
        if self.connected or now - self.last_try < self.backoff:
            return
        self.last_try = now
        try:
            self.feed.connect()
        except AuthRejected as exc:
            log.error("Sensi rejected the refresh token: %s", exc)
            self._drop()
            self.login_bad, self.backoff = True, MAX_BACKOFF
        except Exception as exc:  # the cloud is down or unreachable: keep trying
            log.warning("Can't connect to Sensi: %s", exc)
            self._drop()
            self.backoff = min(self.backoff * 2, MAX_BACKOFF)
        else:
            self.connected, self.login_bad, self.backoff, self.quiet_since = True, False, TICK, now

    def _drop(self) -> None:
        self.feed.disconnect()
        self.connected = False

    def _drain(self) -> bool:
        """Handle every waiting state event. True if any came in."""
        got = False
        while True:
            try:
                items = self.feed.events.get_nowait()
            except queue.Empty:
                return got
            got = True
            self.last_data = self.quiet_since = time.time()
            self.notifier.changes(self.watcher.update(items if isinstance(items, list) else [items]))

    def problems(self, now: float) -> list[Finding]:
        found = []
        if self.login_bad:
            found.append(Finding("cloud:login", f"Sensi rejected the login: {LOGIN_ADVICE}", "error"))
        if now - self.last_data >= self.cfg.offline_grace_minutes * 60:
            since = time.strftime("%H:%M", time.localtime(self.last_data))
            found.append(Finding("cloud:silent", f"No data from Sensi's cloud since {since}", "warning"))
        return found + self.watcher.offline(self.cfg.offline_grace_minutes, now)

    def tick(self) -> None:
        now = time.time()
        self._connection(now)
        got = self._drain()
        self.notifier.changes([])  # retries whatever couldn't be delivered
        problems = self.problems(now)
        changed = self.notifier.problems(problems)
        self.runtime.record(problems, None)
        if got or changed:
            self.state.save()

    def run(self) -> None:
        while True:
            health.beat("feed", TICK * 12)
            try:
                self.tick()
            except Exception:  # keep the daemon alive; next tick may succeed
                log.exception("Tick crashed")
            self.runtime.wake.wait(TICK)
            self.runtime.wake.clear()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.from_env()
    state = State(cfg.state_path)
    watcher = Watcher(state, cfg.history_size)
    notifier = Notifier(cfg.notify_urls, cfg.renotify_hours, state, cfg.events, cfg.bot_alert_url, cfg.bus_token)
    runtime = Runtime("sensi", revision=_revision())
    feed = Feed(Tokens(cfg.client_id, cfg.client_secret, cfg.refresh_token, cfg.tokens_path))
    health.reset()
    if cfg.bus_token and cfg.bus_port:
        # No destructive tools, so no approval channel is needed.
        serve_mcp("0.0.0.0", cfg.bus_port, cfg.bus_token, bus_tools(runtime, watcher, state),
                  "sensi-agent", Approvals("sensi", [], mode="client"))
        health.endpoint("agent bus", f"http://127.0.0.1:{cfg.bus_port}/health")
    log.info("Starting: events=%s resync=%ss grace=%smin bus=%s", ",".join(cfg.events), cfg.resync_seconds,
             cfg.offline_grace_minutes, bool(cfg.bus_token))
    Agent(cfg, feed, watcher, notifier, state, runtime).run()


def _revision() -> str:
    try:
        with open(os.path.join(os.path.dirname(__file__), "..", "REVISION"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "dev"


if __name__ == "__main__":
    main()
