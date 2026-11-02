"""What the heat costs: boiler run time from the thermostats, priced from National Grid gas bills.

A modulating boiler burns anything from a tenth of its rating to all of it, so its rating says little
about the gas used. Instead, each bill gives the burn rate: the therms left after the household's
non-heating use, divided by the hours the heat ran in that period. It's an estimate, and it can only
be as good as the assumption that non-heating use stays near the summer level.

National Grid has no official API for this: see README.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import statistics
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as clock

import aiohttp
from py_nationalgrid import InvalidAuthError, NationalGridClient, NationalGridConfig, create_cookie_jar

from .findings import Finding
from .notice import Message, Section
from .state import State
from .watch import GAP, action

log = logging.getLogger(__name__)
logging.getLogger("py_nationalgrid").setLevel(logging.WARNING)  # its debug output names the account

AUTHOR = "Sensi agent"
HEATING = ("heat", "aux")
DAY = 86400
KEEP_DAYS = 800  # two heating seasons and a bit
KEEP_BILLS = 36
RECENT_BILLS = 12  # enough for a full year of seasons, and rates older than that have moved on
FETCH_EVERY = DAY  # a bill arrives monthly, so there's no reason to sign in more often
RETRY_AFTER = 6 * 3600  # not sooner: repeated sign-ins could lock the account
FETCH_TIMEOUT = 300
STALE_AFTER = 3 * DAY  # no successful fetch for this long is worth saying
MIN_COVERAGE = 0.5  # of a bill period, or its run time says too little about its gas
MIN_RUN_HOURS = 5  # summer bills have too little heating to work out a burn rate from


# --- Reading the bills ---

def normalize(records: list[dict]) -> list[dict]:
    """Keeps only the numbers this needs, oldest first: no account, customer or address details."""
    bills = []
    for r in records:
        try:
            bills.append({"start": r["readFromDate"][:10], "end": r["readDate"][:10], "days": int(r["readDays"]),
                          "therms": float(r["totalTherms"]), "delivery": float(r["utilityCharges"]),
                          "supply": float(r["supplierCharges"]), "read": str(r.get("readType") or "")})
        except (KeyError, TypeError, ValueError):
            log.warning("Skipping a gas bill with missing fields")
    return sorted(bills, key=lambda b: b["end"])[-KEEP_BILLS:]


async def _fetch(username: str, password: str) -> list[dict]:
    config = NationalGridConfig(username=username, password=password)
    async with aiohttp.ClientSession(cookie_jar=create_cookie_jar()) as session:
        async with NationalGridClient(config=config, session=session) as client:
            for link in await client.get_linked_accounts():
                account = link["billingAccountId"]
                billing = await client.get_billing_account(account)
                if any(f.get("type") == "GAS" for f in billing.get("fuelTypes", [])):
                    return normalize(await client.get_gas_bill_history(account, str(billing["customerNumber"])))
    return []


def fetch_bills(username: str, password: str) -> list[dict]:
    return asyncio.run(asyncio.wait_for(_fetch(username, password), FETCH_TIMEOUT))


# --- The arithmetic ---

def baseline(bills: list[dict]) -> float | None:
    """Therms a day used for everything but heat: the three lowest recent bills, which are summer ones."""
    rates = sorted(b["therms"] / b["days"] for b in bills[-RECENT_BILLS:] if b["days"] > 0)
    return statistics.mean(rates[:3]) if len(rates) >= 3 else None


def delivery_slope(bills: list[dict]) -> float | None:
    """What delivery charges add per extra therm at heating-season usage. Delivery is tiered, so only
    the busier half of the year shows the rate that heat actually pays."""
    recent = bills[-RECENT_BILLS:]
    if len(recent) < 4:
        return None
    middle = statistics.median(b["therms"] for b in recent)
    busy = [b for b in recent if b["therms"] > middle]
    if len(busy) < 3 or max(b["therms"] for b in busy) - min(b["therms"] for b in busy) < 30:
        return None
    return max(0.0, statistics.linear_regression([b["therms"] for b in busy], [b["delivery"] for b in busy]).slope)


def price_per_therm(bills: list[dict]) -> dict | None:
    """The cost of one more therm: this delivery slope plus the latest bill's supply price."""
    usable = [b for b in bills if b["therms"] > 0]
    if not usable:
        return None
    slope = delivery_slope(usable)
    if slope is None:  # too little history: the busiest bill's delivery rate is the nearest thing
        busiest = max(usable, key=lambda b: b["therms"])
        slope = busiest["delivery"] / busiest["therms"]
    supply = usable[-1]["supply"] / usable[-1]["therms"]
    return {"delivery": round(slope, 3), "supply": round(supply, 3), "total": round(slope + supply, 3),
            "supply_from_bill": usable[-1]["end"]}


def window(days: dict, first: str, stop: str) -> dict:
    """Run time from date `first` up to but not including `stop`, in seconds."""
    total = {"watched": 0.0, "boiler": 0.0, "thermostats": {}}
    for day, entry in days.items():
        if first <= day < stop:
            total["watched"] += entry.get("watched", 0)
            total["boiler"] += entry.get("boiler", 0)
            for name, seconds in entry.get("thermostats", {}).items():
                total["thermostats"][name] = total["thermostats"].get(name, 0) + seconds
    return total


def bill_heating(bill: dict, days: dict, base: float) -> dict | None:
    """One bill's heating therms against its run time, or None if the two can't be compared."""
    if bill["days"] <= 0 or bill["read"].lower() != "actual":  # an estimated read isn't what was burned
        return None
    ran = window(days, bill["start"], bill["end"])
    coverage = min(1.0, ran["watched"] / (bill["days"] * DAY))
    hours = ran["boiler"] / 3600
    heating = max(0.0, bill["therms"] - base * bill["days"])
    if coverage < MIN_COVERAGE or hours < MIN_RUN_HOURS or heating <= 0:
        return None
    # Heating outside the watched part of the period has no run time to go with it.
    return {"bill_end": bill["end"], "heating_therms": round(heating, 1), "run_hours": round(hours, 1),
            "coverage": round(coverage, 2), "therms_per_hour": round(heating * coverage / hours, 3)}


def calibrate(bills: list[dict], days: dict) -> dict | None:
    """The burn rate from the newest bill that has one. Boilers fire lower in mild weather, so the
    newest is the closest to the weather being estimated."""
    base = baseline(bills)
    if base is None:
        return None
    for bill in reversed(bills):
        found = bill_heating(bill, days, base)
        if found:
            return found
    return None


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.0f} h" if seconds >= 3600 else f"{seconds / 60:.0f} min"


def _short(day: str) -> str:
    return f"{date.fromisoformat(day):%b} {date.fromisoformat(day).day}"


class Gas:
    def __init__(self, state: State, username: str, password: str, send, report_day: int = 6, report_hour: int = 8):
        self.state = state
        self.username, self.password = username, password
        self.send = send  # delivers a Message, True when it got through
        self.report_day, self.report_hour = report_day, report_hour
        self.results: queue.Queue = queue.Queue()
        self.fetching = False

    @property
    def data(self) -> dict:
        return self.state["gas"]

    # --- Run time ---

    def observe(self, thermostats: dict, now: float | None = None) -> None:
        """Called with each state event. The time since the last one counts as whatever was happening
        then: the boiler for either thermostat calling, since they share it. Time the agent wasn't
        watching counts as neither, so a bill's share of it can be allowed for."""
        now = time.time() if now is None else now
        calling = sorted(e["name"] for e in thermostats.values()
                         if action(e.get("state") or {}) in HEATING and now - e.get("seen", 0) < GAP
                         and str((e.get("state") or {}).get("status")).lower() == "online")
        last = self.data.get("last")
        if last and 0 < now - last["at"] < GAP:
            self._add(last["at"], now, last["calling"])
        self.data["last"] = {"at": now, "calling": calling}

    def _add(self, start: float, end: float, calling: list[str]) -> None:
        days = self.data.setdefault("days", {})
        while start < end:
            local = datetime.fromtimestamp(start)
            midnight = datetime.combine(local.date() + timedelta(days=1), clock()).timestamp()
            part = min(end, midnight) - start
            entry = days.setdefault(local.date().isoformat(), {"watched": 0, "boiler": 0, "thermostats": {}})
            entry["watched"] += part
            if calling:
                entry["boiler"] += part
                for name in calling:
                    entry["thermostats"][name] = entry["thermostats"].get(name, 0) + part
            start += part
        for old in sorted(days)[:-KEEP_DAYS]:
            del days[old]

    # --- Fetching, off the main loop since a sign-in takes a while ---

    def _worker(self) -> None:
        try:
            self.results.put(("ok", fetch_bills(self.username, self.password)))
        except InvalidAuthError:
            self.results.put(("rejected", None))
        except Exception as exc:  # the class only: messages from a login flow can carry details
            log.warning("Can't read National Grid: %s", type(exc).__name__)
            self.results.put(("failed", None))

    def _fetch_if_due(self, now: float) -> bool:
        changed = False
        try:
            outcome, bills = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self.fetching, changed = False, True
            self.data["rejected"] = outcome == "rejected"
            self.data["next_fetch"] = now + (RETRY_AFTER if outcome == "failed" else FETCH_EVERY)
            if outcome == "ok":
                self.data["ok_at"] = now
                if bills:
                    self.data["bills"] = bills
                log.info("National Grid: %d gas bills", len(bills))
        if not self.fetching and now >= self.data.get("next_fetch", 0):
            self.fetching = True
            threading.Thread(target=self._worker, name="national-grid", daemon=True).start()
        return changed

    # --- What gets said ---

    def estimate(self, ran: dict) -> dict:
        """Run time in a period turned into therms and dollars, as far as the bills allow."""
        bills = self.data.get("bills", [])
        rate, price = calibrate(bills, self.data.get("days", {})), price_per_therm(bills)
        out = {"boiler_hours": round(ran["boiler"] / 3600, 1), "watched_days": round(ran["watched"] / DAY, 1),
               "hours_by_thermostat": {k: round(v / 3600, 1) for k, v in sorted(ran["thermostats"].items())},
               "burn_rate": rate, "price_per_therm": price, "estimated_therms": None, "estimated_cost": None}
        if rate:
            therms = ran["boiler"] / 3600 * rate["therms_per_hour"]
            out["estimated_therms"] = round(therms, 1)
            if price:
                out["estimated_cost"] = round(therms * price["total"], 2)
        return out

    def report(self, first: str, stop: str) -> Message:
        ran = window(self.data.get("days", {}), first, stop)
        est = self.estimate(ran)
        span = (date.fromisoformat(stop) - date.fromisoformat(first)).days
        by_thermostat = ", ".join(f"{k} {_hours(v * 3600)}" for k, v in est["hours_by_thermostat"].items())
        lines = [f"Boiler ran {_hours(ran['boiler'])}" + (f" ({by_thermostat})" if by_thermostat else "")]
        if est["estimated_cost"] is not None:
            title = f"🔥 Heating last week: about ${est['estimated_cost']:.0f}"
            lines.append(f"About {est['estimated_therms']:.0f} therms at about "
                         f"${est['price_per_therm']['total']:.2f} each")
            lines.append(f"Burn rate {est['burn_rate']['therms_per_hour']:.2f} therms per hour, "
                         f"from the bill ending {_short(est['burn_rate']['bill_end'])}")
        else:
            title = f"🔥 Heating last week: {_hours(ran['boiler'])} of run time"
            lines.append("The cost starts after the next gas bill: it takes a bill's worth of run time "
                         "to work out what the boiler burns")
        if ran["watched"] < (span - 0.1) * DAY:
            lines.append(f"Watched {ran['watched'] / DAY:.1f} of {span} days, so this can be low")
        return Message(title, "info", [Section("", lines, bullets=False)], AUTHOR, footer="A rough estimate")

    def _report_if_due(self, now: float) -> bool:
        local = datetime.fromtimestamp(now)
        due_day = local.date() - timedelta(days=(local.weekday() - self.report_day) % 7)
        if local < datetime.combine(due_day, clock(self.report_hour)):
            due_day -= timedelta(days=7)
        due = due_day.isoformat()
        if "last_report" not in self.data:  # the first report waits for a whole week of run time
            self.data["last_report"] = due
            return True
        if self.data["last_report"] == due:
            return False
        first = (due_day - timedelta(days=7)).isoformat()
        if not self.send(self.report(first, due)):
            return False
        self.data["last_report"] = due
        return True

    def bill_message(self, bill: dict) -> Message:
        bills = self.data.get("bills", [])
        days = self.data.get("days", {})
        cost = bill["delivery"] + bill["supply"]
        lines = [f"{_short(bill['start'])} to {_short(bill['end'])} ({bill['days']} days): "
                 f"delivery ${bill['delivery']:.2f}, supply ${bill['supply']:.2f}"]
        base = baseline(bills)
        found = bill_heating(bill, days, base) if base is not None else None
        if found:
            lines.append(f"Heating about {found['heating_therms']:.0f} therms, the rest hot water and cooking")
            lines.append(f"Boiler ran {found['run_hours']:.0f} h, so about {found['therms_per_hour']:.2f} therms per hour")
            earlier = calibrate([b for b in bills if b["end"] < bill["end"]], days)
            if earlier:
                expected = found["run_hours"] * earlier["therms_per_hour"]
                actual = found["run_hours"] * found["therms_per_hour"]
                lines.append(f"The estimate was about {expected:.0f} therms and the bill works out to about {actual:.0f}")
        return Message(f"🧾 Gas bill: {bill['therms']:.0f} therms, ${cost:.2f}", "info",
                       [Section("", lines, bullets=False)], AUTHOR)

    def _announce_bill(self) -> bool:
        bills = self.data.get("bills", [])
        if not bills:
            return False
        newest = bills[-1]
        if "announced" not in self.data:  # bills from before the agent knew about them aren't news
            self.data["announced"] = newest["end"]
            return True
        if newest["end"] <= self.data["announced"] or not self.send(self.bill_message(newest)):
            return False
        self.data["announced"] = newest["end"]
        return True

    def tick(self, now: float | None = None) -> bool:
        """Fetches, announces and reports whatever is due. True if there's anything to save."""
        now = time.time() if now is None else now
        changed = self._fetch_if_due(now)
        changed = self._announce_bill() or changed
        return self._report_if_due(now) or changed

    def problems(self, now: float | None = None) -> list[Finding]:
        now = time.time() if now is None else now
        if self.data.get("rejected"):
            return [Finding("gas:login", "National Grid rejected the login: check NATIONALGRID_USERNAME and "
                            "NATIONALGRID_PASSWORD", "warning")]
        ok_at = self.data.get("ok_at")
        if ok_at and now - ok_at >= STALE_AFTER:
            since = time.strftime("%b %d", time.localtime(ok_at))
            return [Finding("gas:stale", f"No gas bill data from National Grid since {since}", "warning")]
        return []

    def status(self, days: int = 7, now: float | None = None) -> dict:
        """The last `days` days including today, with the bills behind the numbers."""
        now = time.time() if now is None else now
        days = max(1, min(int(days), 365))
        today = datetime.fromtimestamp(now).date()
        first = today - timedelta(days=days - 1)
        bills = self.data.get("bills", [])
        base = baseline(bills)
        return {"from": first.isoformat(), "through": today.isoformat(),
                **self.estimate(window(self.data.get("days", {}), first.isoformat(),
                                       (today + timedelta(days=1)).isoformat())),
                "non_heating_therms_per_day": round(base, 2) if base is not None else None,
                "recent_bills": bills[-RECENT_BILLS:],
                "note": "A rough estimate: boiler hours from the thermostats, burn rate and prices from the bills. "
                        "Cost is for the extra gas heat uses, not a share of fixed charges."}
