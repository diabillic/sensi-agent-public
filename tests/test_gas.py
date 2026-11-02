import time
from datetime import datetime

import pytest

from sensi_agent import gas as gas_module
from sensi_agent.__main__ import Agent, bus_tools
from sensi_agent.bus import Runtime
from sensi_agent.config import Config
from sensi_agent.gas import DAY, Gas, baseline, calibrate, delivery_slope, normalize, price_per_therm, window
from sensi_agent.state import State
from sensi_agent.watch import Watcher

from .test_agent import FakeFeed, event, notifier


def ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def bill(start, end, days, therms, delivery=50.0, supply=10.0, read="Actual"):
    return {"start": start, "end": end, "days": days, "therms": therms, "delivery": delivery, "supply": supply,
            "read": read}


# Invented figures: a low summer, a busy winter, with delivery rising by 0.50 a therm when busy.
YEAR = [bill("2025-06-01", "2025-07-01", 30, 15), bill("2025-07-01", "2025-08-01", 31, 15),
        bill("2025-08-01", "2025-09-01", 31, 15), bill("2025-09-01", "2025-10-01", 30, 30),
        bill("2025-10-01", "2025-11-01", 31, 60, 100, 40), bill("2025-11-01", "2025-12-01", 30, 100, 120, 80),
        bill("2025-12-01", "2026-01-01", 31, 160, 150, 120), bill("2026-01-01", "2026-02-01", 31, 180, 160, 150)]


@pytest.fixture
def state(tmp_path):
    return State(str(tmp_path / "state.json"))


class Mail:
    def __init__(self, ok=True):
        self.ok, self.messages = ok, []

    def __call__(self, msg):
        self.messages.append(msg)
        return self.ok


@pytest.fixture
def mail():
    return Mail()


@pytest.fixture
def gas(state, mail):
    return Gas(state, "user", "secret", mail)


def thermostat(name, heat=0, seen=0.0, status="online", cool=0):
    return {"name": name, "seen": seen, "state": {"status": status, "demand_status": {"heat": heat, "cool": cool}}}


# --- Run time ---

def test_time_counts_as_what_was_happening_before(gas):
    start = ts("2026-01-10T12:00:00")
    gas.observe({"a": thermostat("Hallway", 100, start)}, start)
    gas.observe({"a": thermostat("Hallway", 0, start + 60)}, start + 60)
    gas.observe({"a": thermostat("Hallway", 0, start + 120)}, start + 120)
    day = gas.data["days"]["2026-01-10"]
    assert day["boiler"] == 60 and day["watched"] == 120 and day["thermostats"] == {"Hallway": 60}


def test_both_thermostats_calling_count_once_for_the_boiler(gas):
    start = ts("2026-01-10T12:00:00")
    both = {"a": thermostat("Hallway", 100, start), "b": thermostat("Upstairs", 50, start)}
    gas.observe(both, start)
    gas.observe({k: {**v, "seen": start + 60} for k, v in both.items()}, start + 60)
    day = gas.data["days"]["2026-01-10"]
    assert day["boiler"] == 60 and day["thermostats"] == {"Hallway": 60, "Upstairs": 60}


def test_ac_burns_no_gas(gas):
    start = ts("2026-07-10T12:00:00")
    gas.observe({"a": thermostat("Hallway", cool=100, seen=start)}, start)
    gas.observe({"a": thermostat("Hallway", cool=100, seen=start + 60)}, start + 60)
    assert gas.data["days"]["2026-07-10"]["boiler"] == 0


def test_an_outage_counts_as_neither(gas):
    start = ts("2026-01-10T12:00:00")
    gas.observe({"a": thermostat("Hallway", 100, start)}, start)
    gas.observe({"a": thermostat("Hallway", 100, start + 3600)}, start + 3600)
    assert "days" not in gas.data


def test_offline_or_stale_thermostat_is_not_calling(gas):
    start = ts("2026-01-10T12:00:00")
    gas.observe({"a": thermostat("Hallway", 100, start, status="offline")}, start)
    assert gas.data["last"]["calling"] == []
    gas.observe({"a": thermostat("Hallway", 100, start - 3600)}, start + 60)
    assert gas.data["last"]["calling"] == []


def test_a_run_over_midnight_splits(gas):
    start = ts("2026-01-10T23:59:30")
    gas.observe({"a": thermostat("Hallway", 100, start)}, start)
    gas.observe({"a": thermostat("Hallway", 100, start + 60)}, start + 60)
    assert gas.data["days"]["2026-01-10"]["boiler"] == 30 and gas.data["days"]["2026-01-11"]["boiler"] == 30


def test_run_time_survives_a_restart(state, mail):
    gas = Gas(state, "u", "p", mail)
    start = ts("2026-01-10T12:00:00")
    gas.observe({"a": thermostat("Hallway", 100, start)}, start)
    gas.observe({"a": thermostat("Hallway", 100, start + 60)}, start + 60)
    state.save()
    assert Gas(State(state.path), "u", "p", mail).data["days"]["2026-01-10"]["boiler"] == 60


# --- The arithmetic ---

def test_normalize_keeps_only_the_numbers_oldest_first():
    record = {"readFromDate": "2026-01-01T00:00:00", "readDate": "2026-02-01T00:00:00", "readDays": 31, "readType": "Actual",
              "totalTherms": 100.0, "utilityCharges": 80.0, "supplierCharges": 60.0, "latePayment": 5.0,
              "billingAccountId": "should not be kept", "customerNumber": "nor this"}
    older = {**record, "readFromDate": "2025-12-01T00:00:00", "readDate": "2026-01-01T00:00:00"}
    bills = normalize([record, older, {"readDate": "broken"}])
    assert [b["end"] for b in bills] == ["2026-01-01", "2026-02-01"]
    assert set(bills[0]) == {"start", "end", "days", "therms", "delivery", "supply", "read"}


def test_baseline_is_the_three_lowest_recent_bills():
    assert baseline(YEAR) == pytest.approx((15 / 30 + 15 / 31 + 15 / 31) / 3)
    assert baseline(YEAR[:2]) is None


def test_delivery_slope_comes_from_the_busy_bills_only():
    assert delivery_slope(YEAR) == pytest.approx(0.5, abs=0.02)  # the 2-a-therm summer tier would inflate it
    assert delivery_slope(YEAR[:3]) is None


def test_price_is_the_slope_plus_the_latest_supply_price():
    price = price_per_therm(YEAR)
    assert price["supply"] == pytest.approx(150 / 180, abs=0.001)
    assert price["total"] == pytest.approx(price["delivery"] + price["supply"], abs=0.002)
    assert price["supply_from_bill"] == "2026-02-01"


def test_price_with_little_history_uses_the_busiest_bill():
    assert price_per_therm(YEAR[-1:])["delivery"] == pytest.approx(160 / 180, abs=0.001)


def test_price_without_bills():
    assert price_per_therm([]) is None


def days_covering(first, count, boiler_hours_a_day, watched=DAY):
    start = datetime.fromisoformat(first)
    return {datetime.fromtimestamp(start.timestamp() + i * DAY + 3600).date().isoformat():
            {"watched": watched, "boiler": boiler_hours_a_day * 3600, "thermostats": {}} for i in range(count)}


def test_calibration_divides_heating_therms_by_run_hours():
    found = calibrate(YEAR, days_covering("2026-01-01", 31, 4))
    base = baseline(YEAR)
    assert found["bill_end"] == "2026-02-01" and found["run_hours"] == 124
    assert found["therms_per_hour"] == pytest.approx((180 - base * 31) / 124, abs=0.001)


def test_calibration_scales_a_partly_watched_bill():
    full = calibrate(YEAR, days_covering("2026-01-01", 31, 4))
    part = calibrate(YEAR, days_covering("2026-01-08", 24, 4))
    assert part["coverage"] == pytest.approx(24 / 31, abs=0.01)
    assert part["therms_per_hour"] == pytest.approx(full["therms_per_hour"], abs=0.002)


def test_calibration_needs_enough_of_the_period():
    assert calibrate(YEAR, days_covering("2026-01-25", 7, 4)) is None


def test_calibration_needs_enough_run_time():
    assert calibrate(YEAR, days_covering("2026-01-01", 31, 0.1)) is None


def test_calibration_skips_an_estimated_read_for_an_earlier_actual_one():
    bills = YEAR[:-1] + [{**YEAR[-1], "read": "Estimated"}]
    days = {**days_covering("2025-12-01", 31, 3), **days_covering("2026-01-01", 31, 4)}
    assert calibrate(bills, days)["bill_end"] == "2026-01-01"


def test_window_is_half_open():
    days = {"2026-01-01": {"watched": 1, "boiler": 1, "thermostats": {"A": 1}},
            "2026-01-02": {"watched": 2, "boiler": 2, "thermostats": {"A": 2}}}
    assert window(days, "2026-01-01", "2026-01-02") == {"watched": 1, "boiler": 1, "thermostats": {"A": 1}}


# --- Reports ---

SUNDAY = ts("2026-02-08T09:00:00")


def calibrated(gas):
    gas.data["bills"] = YEAR
    gas.data["announced"] = YEAR[-1]["end"]
    gas.data["next_fetch"] = SUNDAY + DAY
    gas.data["days"] = {**days_covering("2026-01-01", 31, 4),
                        **{d: {**v, "thermostats": {"Hallway": v["boiler"]}}
                           for d, v in days_covering("2026-02-01", 7, 5).items()}}


def test_first_report_waits_for_a_whole_week(gas, mail):
    calibrated(gas)
    assert gas.tick(SUNDAY) and not mail.messages
    assert gas.data["last_report"] == "2026-02-08"


def test_weekly_report_with_a_cost(gas, mail):
    calibrated(gas)
    gas.data["last_report"] = "2026-02-01"
    assert gas.tick(SUNDAY)
    msg = mail.messages[0]
    rate = calibrate(YEAR, gas.data["days"])["therms_per_hour"]
    cost = 35 * rate * price_per_therm(YEAR)["total"]
    assert msg.title == f"🔥 Heating last week: about ${cost:.0f}"
    body = msg.markdown()
    assert "Boiler ran 35 h (Hallway 35 h)" in body and "Watched" not in body
    assert not gas.tick(SUNDAY + 60) and len(mail.messages) == 1


def test_report_before_the_first_calibration_gives_hours(gas, mail):
    gas.data["bills"] = YEAR
    gas.data["announced"] = YEAR[-1]["end"]
    gas.data["next_fetch"] = SUNDAY + DAY
    gas.data["last_report"] = "2026-02-01"
    gas.data["days"] = days_covering("2026-02-01", 7, 5)
    gas.tick(SUNDAY)
    assert mail.messages[0].title == "🔥 Heating last week: 35 h of run time"
    assert "after the next gas bill" in mail.messages[0].markdown()


def test_report_says_when_part_of_the_week_was_not_watched(gas, mail):
    calibrated(gas)
    gas.data["last_report"] = "2026-02-01"
    del gas.data["days"]["2026-02-03"]
    gas.tick(SUNDAY)
    assert "Watched 6.0 of 7 days" in mail.messages[0].markdown()


def test_report_not_due_before_the_hour(gas, mail):
    calibrated(gas)
    gas.data["last_report"] = "2026-02-01"
    assert not gas.tick(ts("2026-02-08T07:59:00")) and not mail.messages


def test_undelivered_report_is_retried(state):
    mail = Mail(ok=False)
    gas = Gas(state, "u", "p", mail)
    calibrated(gas)
    gas.data["last_report"] = "2026-02-01"
    gas.tick(SUNDAY)
    assert gas.data["last_report"] == "2026-02-01"
    mail.ok = True
    gas.tick(SUNDAY + 5)
    assert gas.data["last_report"] == "2026-02-08" and len(mail.messages) == 2


def test_existing_bills_are_not_announced(gas, mail):
    calibrated(gas)
    del gas.data["announced"]
    gas.data["last_report"] = "2026-02-08"
    assert gas.tick(SUNDAY) and not mail.messages
    assert gas.data["announced"] == YEAR[-1]["end"]


def test_new_bill_is_announced_with_the_estimate_it_replaces(gas, mail):
    calibrated(gas)
    gas.data["last_report"] = "2026-02-08"
    new = bill("2026-02-01", "2026-03-01", 28, 170, 155, 140)
    gas.data["bills"] = YEAR + [new]
    gas.data["days"].update(days_covering("2026-02-01", 28, 5))
    gas.tick(SUNDAY)
    msg = mail.messages[0]
    assert msg.title == "🧾 Gas bill: 170 therms, $295.00"
    body = msg.markdown()
    assert "Boiler ran 140 h" in body and "The estimate was about" in body
    assert gas.data["announced"] == "2026-03-01"


# --- Fetching ---

def settle(gas, now):
    """Starts a fetch, waits for its thread, and takes in the result."""
    gas.tick(now)
    for _ in range(200):
        if not gas.results.empty():
            break
        time.sleep(0.01)
    gas.tick(now)


def test_fetch_stores_bills_and_waits_a_day(gas, monkeypatch):
    calls = []
    monkeypatch.setattr(gas_module, "fetch_bills", lambda u, p: calls.append((u, p)) or YEAR)
    settle(gas, SUNDAY)
    assert calls == [("user", "secret")] and gas.data["bills"] == YEAR
    assert gas.data["next_fetch"] == SUNDAY + DAY
    gas.tick(SUNDAY + 60)
    assert len(calls) == 1


def test_rejected_login_is_reported_and_not_hammered(gas, monkeypatch):
    def rejected(u, p):
        raise gas_module.InvalidAuthError("nope")

    monkeypatch.setattr(gas_module, "fetch_bills", rejected)
    settle(gas, SUNDAY)
    assert gas.data["next_fetch"] == SUNDAY + DAY
    assert [f.key for f in gas.problems(SUNDAY)] == ["gas:login"]


def test_failure_retries_sooner_and_keeps_the_bills_and_logs_no_detail(gas, monkeypatch, caplog):
    gas.data["bills"] = YEAR

    def broken(u, p):
        raise RuntimeError("account 12345 said no")

    monkeypatch.setattr(gas_module, "fetch_bills", broken)
    settle(gas, SUNDAY)
    assert gas.data["bills"] == YEAR and gas.data["next_fetch"] == SUNDAY + gas_module.RETRY_AFTER
    assert "12345" not in caplog.text and "RuntimeError" in caplog.text


def test_long_gap_in_bill_data_is_reported(gas):
    gas.data["ok_at"] = SUNDAY
    assert gas.problems(SUNDAY + DAY) == []
    assert [f.key for f in gas.problems(SUNDAY + 4 * DAY)] == ["gas:stale"]


# --- Wiring ---

def test_status_covers_the_days_asked_for(gas):
    calibrated(gas)
    out = gas.status(3, ts("2026-02-05T12:00:00"))
    assert out["from"] == "2026-02-03" and out["through"] == "2026-02-05"
    assert out["boiler_hours"] == 15 and out["estimated_cost"] > 0 and len(out["recent_bills"]) == len(YEAR)


def test_bus_tool_only_with_a_login(state, gas):
    def names(g):
        return [t.name for t in bus_tools(Runtime("sensi"), Watcher(state), state, g)]

    assert "sensi_gas_cost" in names(gas) and "sensi_gas_cost" not in names(None)
    assert not [t for t in bus_tools(Runtime("sensi"), Watcher(state), state, gas) if not t.read_only]


def test_agent_feeds_run_time_and_reports_problems(state, gas, tmp_path, monkeypatch):
    monkeypatch.setattr(gas_module, "fetch_bills", lambda u, p: [])
    gas.data["rejected"] = True
    gas.data["next_fetch"] = time.time() + DAY
    config = Config("i", "s", "t", state_path=state.path, tokens_path=str(tmp_path / "t.json"))
    feed = FakeFeed()
    a = Agent(config, feed, Watcher(state), notifier(state), state, Runtime("sensi"), gas)
    feed.events.put(event(demand={"heat": 100}))
    a.tick()
    assert gas.data["last"]["calling"] == ["Hallway"]
    assert "National Grid rejected" in a.notifier.outbox.messages[0].markdown()


@pytest.mark.parametrize("env", [{"NATIONALGRID_USERNAME": "u"}, {"NATIONALGRID_PASSWORD": "p"},
                                 {"GAS_REPORT_DAY": "someday"}, {"GAS_REPORT_HOUR": "24"}])
def test_bad_gas_settings_stop_the_start(monkeypatch, env):
    for key in ("SENSI_CLIENT_ID", "SENSI_CLIENT_SECRET", "SENSI_REFRESH_TOKEN"):
        monkeypatch.setenv(key, "x")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit):
        Config.from_env()


def test_gas_settings(monkeypatch):
    for key in ("SENSI_CLIENT_ID", "SENSI_CLIENT_SECRET", "SENSI_REFRESH_TOKEN"):
        monkeypatch.setenv(key, "x")
    monkeypatch.setenv("NATIONALGRID_USERNAME", "u")
    monkeypatch.setenv("NATIONALGRID_PASSWORD", " p ")
    monkeypatch.setenv("GAS_REPORT_DAY", "Monday")
    monkeypatch.setenv("GAS_REPORT_HOUR", "7")
    cfg = Config.from_env()
    assert cfg.nationalgrid_password == " p " and cfg.gas_report_day == 0 and cfg.gas_report_hour == 7
