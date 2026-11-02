"""Small JSON state file so decisions survive restarts (what was notified, the thermostats, recent changes)."""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any


class State:
    # notified: alert dedupe · thermostats: last state seen per thermostat · history: {"changes": [...]}
    # gas: boiler run time per day, gas bills, and what's been reported
    SECTIONS = ("notified", "thermostats", "history", "gas")

    def __init__(self, path: str):
        self.path = path
        self.data: dict[str, dict[str, Any]] = {s: {} for s in self.SECTIONS}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                for s in self.SECTIONS:
                    self.data[s] = loaded.get(s, {})
            except (OSError, ValueError):
                pass  # corrupt state just means everything looks new once

    def save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def __getitem__(self, section: str) -> dict[str, Any]:
        return self.data[section]
