import copy
import json
import queue
import threading

import pytest
import requests

from sensi_agent import sensi
from sensi_agent.__main__ import Agent, bus_tools
from sensi_agent.bus import Runtime
from sensi_agent.config import Config
from sensi_agent.notify import Notifier
from sensi_agent.sensi import AuthRejected, Tokens
from sensi_agent.state import State
from sensi_agent.watch import Watcher, action

# A state event as Sensi sends it; the name and id are placeholders.
IDLE_HEAT = {"status": "online", "display_temp": 68.5, "display_scale": "f", "humidity": 40, "operating_mode": "heat",
             "current_heat_temp": 70, "current_cool_temp": 76, "hold_mode": "off", "scheduling": "off", "fan_mode": "auto",
             "demand_status": {"heat": 0, "fan": 0, "cool": 0, "aux": 0, "last": "heat", "last_start": None}}


def event(ident="aa-bb", name="Hallway", **changes):
    state = copy.deepcopy(IDLE_HEAT)
    demand = changes.pop("demand", None)
    state.update(changes)
    if demand:
        state["demand_status"].update(demand)
    return [{"icd_id": ident, "registration": {"name": name}, "state": state}]


@pytest.fixture
def state(tmp_path):
    return State(str(tmp_path / "state.json"))


@pytest.fixture
def watcher(state):
    w = Watcher(state)
    assert w.update(event(), now=1000) == []  # the first sighting is the baseline
    return w


def kinds(changes):
    return [c.kind for c in changes]


def test_action_reads_demand_status():
    assert action(IDLE_HEAT) == "idle"
    assert action({"demand_status": {"heat": 50}}) == "heat"
    assert action({"demand_status": {"cool": 100, "fan": 100}}) == "cool"
    assert action({"demand_status": {"aux": 100}}) == "aux"
    assert action({}) == "idle"


def test_heat_on_then_off_with_run_time(watcher):
    on = watcher.update(event(demand={"heat": 100, "last_start": 1000}), now=1060)
    assert kinds(on) == ["hvac_on"]
    assert on[0].headline == "Heat on"
    assert on[0].detail == "Room 68.5°F, set to 70°F"
    watcher.update(event(demand={"heat": 100}), now=1060 + 60)  # the resync keeps it current
    watcher.update(event(demand={"heat": 100}), now=1060 + 660)
    off = watcher.update(event(display_temp=70.0), now=1060 + 720)
    assert kinds(off) == ["hvac_off"]
    assert off[0].headline == "Heat off"
    assert off[0].detail == "Room 70.0°F, ran 12 min"


def test_ac_uses_the_cool_set_temperature(state):
    w = Watcher(state)
    w.update(event(operating_mode="cool"), now=1000)
    on = w.update(event(operating_mode="cool", demand={"cool": 100}), now=1060)
    assert on[0].headline == "AC on"
    assert on[0].detail == "Room 68.5°F, set to 76°F"


def test_stage_change_is_not_a_new_cycle(watcher):
    watcher.update(event(demand={"heat": 50}), now=1060)
    assert watcher.update(event(demand={"heat": 100}), now=1120) == []


def test_run_time_left_out_after_a_gap(watcher):
    watcher.update(event(demand={"heat": 100}), now=1060)
    off = watcher.update(event(), now=1060 + 3600)
    assert off[0].detail == "Room 68.5°F"


def test_set_temperature_change(watcher):
    changes = watcher.update(event(current_heat_temp=72), now=1060)
    assert kinds(changes) == ["setpoint"]
    assert changes[0].headline == "Set temperature 70°F → 72°F"


def test_set_temperature_notes_what_is_in_charge(watcher):
    assert watcher.update(event(current_heat_temp=72, scheduling="on"), now=1060)[0].detail.endswith("schedule running")
    assert watcher.update(event(current_heat_temp=73, hold_mode="temporary"), now=1120)[0].detail.endswith("hold on")


def test_the_unused_set_temperature_is_ignored(watcher):
    assert watcher.update(event(current_cool_temp=80), now=1060) == []


def test_auto_mode_names_the_side_that_moved(state):
    w = Watcher(state)
    w.update(event(operating_mode="auto"), now=1000)
    changes = w.update(event(operating_mode="auto", current_cool_temp=78), now=1060)
    assert changes[0].headline == "Set temperature cool 76°F → 78°F"


def test_mode_change_is_one_change(watcher):
    changes = watcher.update(event(operating_mode="cool"), now=1060)
    assert kinds(changes) == ["mode"]
    assert changes[0].headline == "Mode changed from heat to cool"
    assert kinds(watcher.update(event(operating_mode="off"), now=1120)) == ["mode"]


def test_room_temperature_alone_is_not_a_change(watcher):
    assert watcher.update(event(display_temp=69.0, humidity=41), now=1060) == []


def test_partial_state_keeps_the_rest(watcher):
    item = {"icd_id": "aa-bb", "state": {"current_heat_temp": 71}}
    assert kinds(watcher.update([item], now=1060)) == ["setpoint"]
    assert watcher.thermostats["aa-bb"]["state"]["operating_mode"] == "heat"


def test_event_without_state_only_names_the_thermostat(state):
    w = Watcher(state)
    assert w.update([{"icd_id": "aa-bb", "registration": {"name": "Hallway"}}], now=1000) == []
    assert w.update(event(), now=1060) == []
    assert w.thermostats["aa-bb"]["name"] == "Hallway"


def test_change_while_away_is_found_after_restart(state, watcher):
    state.save()
    again = Watcher(State(state.path))
    assert kinds(again.update(event(operating_mode="cool"), now=2000)) == ["mode"]


def test_history_is_bounded(state):
    w = Watcher(state, history_size=2)
    w.update(event(), now=1000)
    for i in range(5):
        w.update(event(current_heat_temp=71 + i), now=1100 + i)
    history = state["history"]["changes"]
    assert [c["text"].split(" (")[0] for c in history] == ["Set temperature 73°F → 74°F", "Set temperature 74°F → 75°F"]


def test_offline_after_grace(watcher):
    watcher.update(event(status="offline"), now=2000)
    assert watcher.offline(15, now=2000 + 14 * 60) == []
    assert [f.summary for f in watcher.offline(15, now=2000 + 15 * 60)] == ["Hallway is offline"]
    watcher.update(event(), now=4000)
    assert watcher.offline(15, now=9000) == []


# --- Notifications ---

class Sent:
    def __init__(self, ok=True):
        self.ok, self.messages = ok, []

    def send(self, msg):
        self.messages.append(msg)
        return self.ok


def notifier(state, events=("hvac_on", "hvac_off", "setpoint", "mode"), ok=True):
    n = Notifier([], 24, state, list(events))
    n.outbox = Sent(ok)
    return n


def test_one_change_is_one_message(state, watcher):
    n = notifier(state)
    n.changes(watcher.update(event(demand={"heat": 100}), now=1060))
    msg = n.outbox.messages[0]
    assert msg.title == "🔥 Hallway: Heat on"
    assert "Room 68.5°F, set to 70°F" in msg.markdown()


def test_several_changes_share_a_message(state, watcher):
    n = notifier(state)
    n.changes(watcher.update(event(operating_mode="cool", demand={"cool": 100}), now=1060))
    assert len(n.outbox.messages) == 1
    assert n.outbox.messages[0].title.endswith("Hallway: 2 changes")


def test_events_can_be_turned_off(state, watcher):
    n = notifier(state, events=("setpoint", "mode"))
    n.changes(watcher.update(event(demand={"heat": 100}), now=1060))
    assert n.outbox.messages == []
    assert state["history"]["changes"][0]["kind"] == "hvac_on"  # the assistant still sees it


def test_undelivered_changes_are_retried(state, watcher):
    n = notifier(state, ok=False)
    n.changes(watcher.update(event(current_heat_temp=72), now=1060))
    assert len(n.unsent) == 1
    n.outbox.ok = True
    n.changes([])
    assert n.unsent == [] and len(n.outbox.messages) == 2
    assert "· at " in n.outbox.messages[1].markdown()  # delivered late, so it says when


def test_problem_reported_once_then_cleared(state, watcher):
    n = notifier(state)
    watcher.update(event(status="offline"), now=2000)
    found = watcher.offline(0, now=2000)
    assert n.problems(found) is True
    assert n.problems(found) is False
    assert len(n.outbox.messages) == 1
    assert n.problems([]) is True
    assert "Hallway is offline" in n.outbox.messages[1].markdown()


def test_undelivered_problem_is_not_recorded(state):
    from sensi_agent.findings import Finding
    n = notifier(state, ok=False)
    assert n.problems([Finding("x", "broken")]) is False
    assert state["notified"] == {}


# --- Tokens ---

class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self.body = status, body or {}

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_refresh_keeps_the_replacement_token(tmp_path, monkeypatch):
    path = str(tmp_path / "tokens.json")
    seen = []

    def post(url, data, timeout):
        seen.append(data["refresh_token"])
        return Resp(body={"access_token": "A", "refresh_token": "second", "expires_in": 3600})

    monkeypatch.setattr(sensi.requests, "post", post)
    t = Tokens("id", "secret", "first", path)
    assert t.headers() == {"Authorization": "bearer A"}
    assert t.headers() == {"Authorization": "bearer A"}  # still valid: no second refresh
    assert seen == ["first"]
    Tokens("id", "secret", "first", path).refresh()  # a restart uses the replacement
    assert seen == ["first", "second"]
    assert "first" not in open(path).read()  # only a hash of the environment's token


def test_a_new_token_in_the_environment_takes_over(tmp_path, monkeypatch):
    path = str(tmp_path / "tokens.json")
    with open(path, "w") as fh:
        json.dump({"seed_hash": Tokens("i", "s", "old", path).seed_hash, "refresh_token": "rotated"}, fh)
    assert Tokens("i", "s", "old", path).refresh_token == "rotated"
    assert Tokens("i", "s", "new", path).refresh_token == "new"


@pytest.mark.parametrize("status", [400, 401, 403])
def test_rejected_token(tmp_path, monkeypatch, status):
    monkeypatch.setattr(sensi.requests, "post", lambda *a, **k: Resp(status))
    with pytest.raises(AuthRejected):
        Tokens("i", "s", "t", str(tmp_path / "t.json")).refresh()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_server_trouble_is_not_a_bad_token(tmp_path, monkeypatch, status):
    monkeypatch.setattr(sensi.requests, "post", lambda *a, **k: Resp(status))
    with pytest.raises(requests.RequestException):
        Tokens("i", "s", "t", str(tmp_path / "t.json")).refresh()


# --- The main loop ---

class FakeFeed:
    def __init__(self):
        self.events = queue.Queue()
        self.closed = threading.Event()
        self.connects, self.disconnects, self.fail = 0, 0, None

    def connect(self):
        self.connects += 1
        if self.fail:
            raise self.fail

    def disconnect(self):
        self.disconnects += 1


def agent(state, feed, tmp_path, **cfg):
    config = Config("i", "s", "t", state_path=state.path, tokens_path=str(tmp_path / "t.json"), **cfg)
    return Agent(config, feed, Watcher(state), notifier(state), state, Runtime("sensi"))


def test_agent_reports_changes_and_saves(state, tmp_path):
    feed = FakeFeed()
    a = agent(state, feed, tmp_path)
    feed.events.put(event())
    a.tick()
    assert feed.connects == 1 and a.connected
    feed.events.put(event(demand={"heat": 100}))
    a.tick()
    assert a.notifier.outbox.messages[0].title == "🔥 Hallway: Heat on"
    assert "Hallway" in open(state.path, encoding="utf-8").read()


def test_quiet_connection_is_replaced(state, tmp_path):
    feed = FakeFeed()
    a = agent(state, feed, tmp_path, resync_seconds=60)
    a.tick()
    a.quiet_since -= 61
    a.last_try -= 61
    a.tick()
    assert feed.disconnects == 1 and feed.connects == 2


def test_closed_connection_is_replaced(state, tmp_path):
    feed = FakeFeed()
    a = agent(state, feed, tmp_path)
    a.tick()
    feed.closed.set()
    a.last_try -= 60
    a.tick()
    assert feed.connects == 2


def test_rejected_login_is_reported_at_once(state, tmp_path):
    feed = FakeFeed()
    feed.fail = AuthRejected("HTTP 401")
    a = agent(state, feed, tmp_path)
    a.tick()
    assert "rejected the login" in a.notifier.outbox.messages[0].markdown()
    feed.fail = None
    a.last_try -= 600
    a.tick()
    assert "cloud:login" not in state["notified"]


def test_cloud_trouble_waits_for_the_grace_time(state, tmp_path):
    feed = FakeFeed()
    feed.fail = requests.ConnectionError("down")
    a = agent(state, feed, tmp_path, offline_grace_minutes=15)
    a.tick()
    assert a.notifier.outbox.messages == []
    a.last_data -= 16 * 60
    a.tick()
    assert "No data from Sensi" in a.notifier.outbox.messages[0].markdown()


def test_connected_but_silent_is_reported(state, tmp_path):
    feed = FakeFeed()
    a = agent(state, feed, tmp_path, offline_grace_minutes=15)
    a.tick()
    assert a.connected
    a.last_data -= 16 * 60
    a.tick()
    assert "cloud:silent" in state["notified"]


def test_bus_tools_are_read_only(state, watcher):
    tools = {t.name: t for t in bus_tools(Runtime("sensi"), watcher, state)}
    assert all(t.read_only for t in tools.values())
    status = tools["sensi_status"].fn()
    assert status["thermostats"][0]["name"] == "Hallway"
    assert status["thermostats"][0]["set_temperatures"] == {"heat": 70}
    watcher.update(event(current_heat_temp=72), now=1060)
    assert len(tools["sensi_history"].fn(limit=10, thermostat="hallway")["changes"]) == 1
    assert tools["sensi_history"].fn(thermostat="nope")["changes"] == []


def test_feed_never_sends_commands():
    source = open(sensi.__file__, encoding="utf-8").read()
    assert ".emit(" not in source and ".call(" not in source


def test_config_needs_the_credentials(monkeypatch):
    for name in ("SENSI_CLIENT_ID", "SENSI_CLIENT_SECRET", "SENSI_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        Config.from_env()


def test_config_rejects_an_unknown_event(monkeypatch):
    monkeypatch.setenv("SENSI_CLIENT_ID", "i")
    monkeypatch.setenv("SENSI_CLIENT_SECRET", "s")
    monkeypatch.setenv("SENSI_REFRESH_TOKEN", "t")
    monkeypatch.setenv("NOTIFY_EVENTS", "hvac_on,fan")
    with pytest.raises(SystemExit):
        Config.from_env()
    monkeypatch.setenv("NOTIFY_EVENTS", "hvac_on, mode")
    assert Config.from_env().events == ["hvac_on", "mode"]
