"""Sensi's cloud: OAuth tokens, and the realtime Socket.IO feed its own apps use.

There's no official API for consumer accounts; see README. This module only ever listens: it never
emits a command, so the agent can't change a thermostat.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import tempfile
import threading
import time

import requests
import socketio

log = logging.getLogger(__name__)

OAUTH_URL = "https://oauth.sensiapi.io/token"
SOCKET_URL = "https://rt.sensiapi.io"
SOCKET_PATH = "/thermostat"
QUERY = "?capabilities=operating_mode_settings,fan_mode_settings,indoor_stages,outdoor_stages,degrees_fc"
TIMEOUT = 15
EXPIRY_SKEW = 120  # refresh this many seconds before the access token runs out


class AuthRejected(Exception):
    """Sensi turned the refresh token down: only a new one from the browser fixes it."""


class Tokens:
    """The access token, and the refresh token, which Sensi can replace on every refresh.

    The newest refresh token lives in tokens_path (the data volume), because the one in the
    environment may stop working once it's been replaced. A different SENSI_REFRESH_TOKEN in the
    environment means a new login was pasted in, and takes over.
    """

    def __init__(self, client_id: str, client_secret: str, seed: str, path: str):
        self.client_id, self.client_secret, self.path = client_id, client_secret, path
        self.seed_hash = hashlib.sha256(seed.encode()).hexdigest()
        self.refresh_token, self.access_token, self.expires_at = seed, "", 0.0
        try:
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            if saved.get("seed_hash") == self.seed_hash and saved.get("refresh_token"):
                self.refresh_token = saved["refresh_token"]
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tokens-")  # mkstemp: owner-only
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"seed_hash": self.seed_hash, "refresh_token": self.refresh_token}, fh)
        os.replace(tmp, self.path)

    def headers(self) -> dict:
        if not self.access_token or time.time() >= self.expires_at - EXPIRY_SKEW:
            self.refresh()
        return {"Authorization": "bearer " + self.access_token}

    def refresh(self) -> None:
        """Raises AuthRejected for a bad token, requests.RequestException if Sensi can't be reached."""
        resp = requests.post(OAUTH_URL, timeout=TIMEOUT, data={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "grant_type": "refresh_token", "refresh_token": self.refresh_token})
        if resp.status_code in (400, 401, 403):
            raise AuthRejected(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        body = resp.json()
        self.access_token = body["access_token"]
        self.expires_at = time.time() + int(body.get("expires_in") or 0)
        new = body.get("refresh_token")
        if new and new != self.refresh_token:
            self.refresh_token = new
        self._save()


def is_jwt_expired(data) -> bool:
    return isinstance(data, dict) and data.get("message") == "jwt expired"


class Feed:
    """One connection at a time. State events land on self.events for the main loop to read."""

    def __init__(self, tokens: Tokens):
        self.tokens = tokens
        self.events: queue.Queue = queue.Queue()
        self.closed = threading.Event()
        self.expired = False
        self.sio: socketio.Client | None = None

    def connect(self) -> None:
        """Raises AuthRejected, or requests/socketio errors if the cloud can't be reached."""
        if self.expired:
            self.tokens.refresh()
            self.expired = False
        self.closed.clear()
        # Reconnecting is the main loop's job: it has to refresh the token and report outages.
        sio = self.sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        sio.on("state", self._state)
        sio.on("disconnect", lambda *_: self.closed.set())
        sio.on("connect_error", self._connect_error)
        sio.connect(SOCKET_URL + QUERY, headers=self.tokens.headers(), socketio_path=SOCKET_PATH,
                    transports=["websocket"], wait_timeout=TIMEOUT)

    def _state(self, data=None, *_) -> None:
        self.events.put(data)

    def _connect_error(self, data=None, *_) -> None:
        if is_jwt_expired(data):
            self.expired = True

    def disconnect(self) -> None:
        sio, self.sio = self.sio, None
        if sio:
            try:
                sio.disconnect()
            except Exception as exc:  # already gone
                log.debug("Disconnect: %s", exc)
