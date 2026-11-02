"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

EVENTS = ("hvac_on", "hvac_off", "setpoint", "mode")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        raise SystemExit(f"{name} must be a whole number, got {raw!r}") from None


def _list(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]


@dataclass
class Config:
    # Sensi's OAuth client and this account's refresh token (see README: there is no official API)
    client_id: str
    client_secret: str
    refresh_token: str

    events: list[str] = field(default_factory=lambda: list(EVENTS))  # which changes send a notification
    resync_seconds: int = 60  # reconnect for a full state when the cloud has sent nothing for this long
    offline_grace_minutes: int = 15  # a thermostat or the cloud must be gone this long before it's reported
    history_size: int = 500  # changes kept for the assistant

    state_path: str = "/var/lib/sensi-agent/state.json"
    tokens_path: str = "/var/lib/sensi-agent/tokens.json"
    notify_urls: list[str] = field(default_factory=list)
    renotify_hours: int = 24

    # Agent bus: MCP endpoint for an assistant (Hermes), and where to hand it problems
    bus_token: str = ""
    bus_port: int = 8765
    bot_alert_url: str = ""

    # National Grid login, for the heating cost report: both empty turns it off
    nationalgrid_username: str = ""
    nationalgrid_password: str = ""
    gas_report_day: int = 6  # Monday is 0
    gas_report_hour: int = 8

    @classmethod
    def from_env(cls) -> "Config":
        client_id = os.environ.get("SENSI_CLIENT_ID", "").strip()
        client_secret = os.environ.get("SENSI_CLIENT_SECRET", "").strip()
        refresh_token = os.environ.get("SENSI_REFRESH_TOKEN", "").strip()
        if not client_id or not client_secret or not refresh_token:
            raise SystemExit("SENSI_CLIENT_ID, SENSI_CLIENT_SECRET and SENSI_REFRESH_TOKEN are required")
        events = [e.lower() for e in _list("NOTIFY_EVENTS", ",".join(EVENTS))]
        unknown = sorted(set(events) - set(EVENTS))
        if unknown:
            raise SystemExit(f"NOTIFY_EVENTS: unknown {', '.join(unknown)} (choose from {', '.join(EVENTS)})")
        ng_user = os.environ.get("NATIONALGRID_USERNAME", "").strip()
        ng_password = os.environ.get("NATIONALGRID_PASSWORD", "")
        if bool(ng_user) != bool(ng_password):
            raise SystemExit("NATIONALGRID_USERNAME and NATIONALGRID_PASSWORD go together: set both or neither")
        day = (os.environ.get("GAS_REPORT_DAY", "").strip() or "sun").lower()[:3]
        if day not in WEEKDAYS:
            raise SystemExit(f"GAS_REPORT_DAY must be a day of the week, got {day!r}")
        hour = _int("GAS_REPORT_HOUR", 8)
        if not 0 <= hour <= 23:
            raise SystemExit(f"GAS_REPORT_HOUR must be from 0 to 23, got {hour}")
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            events=events,
            resync_seconds=max(30, _int("RESYNC_SECONDS", 60)),
            offline_grace_minutes=max(0, _int("OFFLINE_GRACE_MINUTES", 15)),
            history_size=max(1, _int("HISTORY_SIZE", 500)),
            state_path=os.environ.get("STATE_PATH", "").strip() or "/var/lib/sensi-agent/state.json",
            tokens_path=os.environ.get("TOKENS_PATH", "").strip() or "/var/lib/sensi-agent/tokens.json",
            notify_urls=_list("NOTIFY_URLS"),
            renotify_hours=_int("RENOTIFY_HOURS", 24),
            bus_token=os.environ.get("AGENT_BUS_TOKEN", "").strip(),
            bus_port=_int("AGENT_BUS_PORT", 8765),
            bot_alert_url=os.environ.get("BOT_ALERT_URL", "").strip(),
            nationalgrid_username=ng_user,
            nationalgrid_password=ng_password,
            gas_report_day=WEEKDAYS.index(day),
            gas_report_hour=hour,
        )
