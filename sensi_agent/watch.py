"""Turns Sensi's state events into changes: HVAC on and off, set temperature, and mode.

A state event carries the thermostat's whole state, and it comes again at every reconnect, so
comparing it with the last one saved also finds what changed while the agent was away.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .findings import Finding
from .state import State

log = logging.getLogger(__name__)

IDLE = "idle"
LABEL = {"heat": "Heat", "cool": "AC", "aux": "Aux heat"}
ICON = {"heat": "🔥", "cool": "❄️", "aux": "🔥", IDLE: "💤", "setpoint": "🌡️", "mode": "🎛️"}
SIDES = {"heat": ("heat",), "aux": ("heat",), "cool": ("cool",), "auto": ("heat", "cool")}
GAP = 5 * 60  # with no state for this long, a run time would count time the agent wasn't watching


@dataclass
class Change:
    thermostat: str
    kind: str  # hvac_on | hvac_off | setpoint | mode
    icon: str
    headline: str
    detail: str
    at: float

    def record(self) -> dict:
        return {"thermostat": self.thermostat, "kind": self.kind, "text": f"{self.headline} ({self.detail})", "at": self.at}


def action(state: dict) -> str:
    """What the HVAC is doing: demand_status gives 0 when off, and 50 or 100 by stage when on."""
    demand = state.get("demand_status") or {}
    for kind in ("cool", "aux", "heat"):  # a stage change within a kind isn't a new cycle
        try:
            if float(demand.get(kind) or 0) > 0:
                return kind
        except (TypeError, ValueError):
            continue
    return IDLE


def setpoints(state: dict) -> dict:
    """The set temperatures this mode actually uses, as side -> value."""
    return {side: state.get(f"current_{side}_temp") for side in SIDES.get(state.get("operating_mode"), ())}


def _temp(value, state: dict) -> str:
    return "?" if value is None else f"{value}°{str(state.get('display_scale') or 'f').upper()}"


def _room(state: dict) -> str:
    return f"Room {_temp(state.get('display_temp'), state)}"


def _targets(state: dict) -> str:
    points = setpoints(state)
    if len(points) == 1:
        return f"set to {_temp(next(iter(points.values())), state)}"
    return ", ".join(f"{side} set to {_temp(v, state)}" for side, v in points.items())


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"


def _detail(state: dict, *extra: str) -> str:
    return ", ".join([_room(state)] + [e for e in extra if e])


def _control(state: dict) -> str:
    """What Sensi says is in charge. It doesn't say who made a change, so this is all there is to go on."""
    if str(state.get("hold_mode") or "off") != "off":
        return "hold on"
    return "schedule running" if state.get("scheduling") == "on" else ""


class Watcher:
    def __init__(self, state: State, history_size: int = 500):
        self.state = state
        self.history_size = history_size

    @property
    def thermostats(self) -> dict:
        return self.state["thermostats"]

    def update(self, items: list[dict], now: float | None = None) -> list[Change]:
        """Apply one state event. The first event on a connection can lack state: nothing to compare yet."""
        now = time.time() if now is None else now
        changes: list[Change] = []
        for item in items or []:
            ident = item.get("icd_id") if isinstance(item, dict) else None
            if not ident:
                continue
            entry = self.thermostats.setdefault(ident, {"name": ident, "state": {}, "seen": 0, "started": None,
                                                        "offline_since": None})
            name = (item.get("registration") or {}).get("name")
            if name:
                entry["name"] = name
            incoming = item.get("state")
            if not incoming:
                continue
            merged = {**entry["state"], **incoming}
            if entry["state"]:
                changes += self._compare(entry, entry["state"], merged, now)
            elif action(merged) != IDLE:  # already running when first seen
                entry["started"] = now
            entry["state"], entry["seen"] = merged, now
            online = str(merged.get("status")).lower() == "online"
            entry["offline_since"] = None if online else entry.get("offline_since") or now
        if changes:
            history = self.state["history"].setdefault("changes", [])
            history += [c.record() for c in changes]
            del history[:-self.history_size]
        return changes

    def _compare(self, entry: dict, old: dict, new: dict, now: float) -> list[Change]:
        changes: list[Change] = []

        def add(kind: str, icon: str, headline: str, detail: str) -> None:
            changes.append(Change(entry["name"], kind, icon, headline, detail, now))

        was, running = action(old), action(new)
        if was != running:
            if was != IDLE:
                ran = ""
                if entry.get("started") and now - entry.get("seen", 0) < GAP:
                    ran = f"ran {_duration(now - entry['started'])}"
                add("hvac_off", ICON[IDLE], f"{LABEL[was]} off", _detail(new, ran))
                entry["started"] = None
            if running != IDLE:
                add("hvac_on", ICON[running], f"{LABEL[running]} on", _detail(new, _targets(new)))
                entry["started"] = now

        if old.get("operating_mode") != new.get("operating_mode"):
            add("mode", ICON["mode"], f"Mode changed from {old.get('operating_mode')} to {new.get('operating_mode')}",
                _detail(new, _targets(new)))
        else:
            before, after = setpoints(old), setpoints(new)
            moved = [f"{side + ' ' if len(after) > 1 else ''}{_temp(before.get(side), old)} → {_temp(value, new)}"
                     for side, value in after.items() if before.get(side) != value]
            if moved:
                add("setpoint", ICON["setpoint"], f"Set temperature {', '.join(moved)}", _detail(new, _control(new)))
        return changes

    def offline(self, grace_minutes: int, now: float | None = None) -> list[Finding]:
        """Thermostats that have reported themselves offline for longer than the grace time."""
        now = time.time() if now is None else now
        found = []
        for ident, entry in self.thermostats.items():
            since = entry.get("offline_since")
            if since and now - since >= grace_minutes * 60:
                found.append(Finding(f"offline:{ident}", f"{entry['name']} is offline", "warning"))
        return found

    def summary(self) -> list[dict]:
        """Every thermostat as the assistant should see it."""
        out = []
        for ident, entry in self.thermostats.items():
            s = entry["state"]
            out.append({"name": entry["name"], "id": ident, "status": s.get("status"),
                        "room_temperature": s.get("display_temp"), "scale": s.get("display_scale"),
                        "humidity": s.get("humidity"), "mode": s.get("operating_mode"), "running": action(s),
                        "running_since": entry.get("started"), "set_temperatures": setpoints(s),
                        "fan_mode": s.get("fan_mode"), "hold": s.get("hold_mode"), "schedule": s.get("scheduling"),
                        "last_update": entry.get("seen")})
        return out
