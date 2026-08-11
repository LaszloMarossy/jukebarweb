"""
Simulates the iOS/Android host app's side of the relay wire protocol -- registration and the
~5s sync loop -- without needing a real device build or simulator. Stands in for "the kiosk"
in integration tests: it owns its own local request store and pushes it out on every sync()
call, exactly like LocalRequestManager (Android) / LocalStorage (iOS) do, per CLAUDE.md's
"host is source of truth; host broadcasts its own state" governing principle.

Deliberately NOT a full reimplementation of either host app's logic (no playback engine, no
Spotify/local-file catalog scanning) -- it only speaks the wire protocol closely enough to drive
the relay through realistic request-lifecycle scenarios. A bug in PlaybackCoordinator.kt or
AppState.swift wouldn't be caught here; see CLAUDE.md's note on this test suite's actual scope.
"""

from __future__ import annotations

import time
import uuid
from typing import Any


class FakeHost:
    def __init__(self, client, jukebar_id: str | None = None, session: str | None = None):
        self.client = client
        self.jukebar_id = jukebar_id or f"test-{uuid.uuid4().hex[:8]}"
        self.session = session or uuid.uuid4().hex
        # Host's own local request store, keyed by request id -- mirrors LocalRequestManager's/
        # LocalStorage's role as the single source of truth for status, echoed out every sync().
        self.requests: dict[str, dict[str, Any]] = {}
        self.up_next: list[dict[str, Any]] = []

    def register(self, catalog: list[dict], **overrides: Any) -> dict:
        body = {
            "jukebar_id": self.jukebar_id,
            "session": self.session,
            "bar_name": "Test Bar",
            "catalog": catalog,
            "bartender_enabled": False,
            "accepting_requests": True,
            "kiosk_mode": "localAndRemote",
            "currency": "USD",
            "price_per_song": 0.0,
            "price_for_three": 0.0,
            **overrides,
        }
        resp = self.client.post("/api/host/register", json=body)
        resp.raise_for_status()
        return resp.json()

    def sync(self, **settings_overrides: Any) -> dict:
        """One sync tick: broadcasts the host's current requests/up_next/settings, adopts any
        newly-known pending requests from the response (mirrors both host apps' "only adopt an
        id we don't already have" rule -- see CLAUDE.md's note on new_requests/echo duplication).
        """
        settings = {
            "bartender_enabled": False,
            "stripe_enabled": False,
            "accepting_requests": True,
            **settings_overrides,
        }
        body = {
            "jukebar_id": self.jukebar_id,
            "session": self.session,
            "requests": list(self.requests.values()),
            "up_next": self.up_next,
            "settings": settings,
        }
        resp = self.client.post("/api/host/sync", json=body)
        resp.raise_for_status()
        data = resp.json()
        for r in data["requests"]:
            if r["id"] not in self.requests:
                self.requests[r["id"]] = {**r, "status": "pending"}
        return data

    def approve_and_inject(self, request_id: str) -> None:
        """Simulate the host actually accepting a request into its live queue (auto-accept, or
        after a bartender's approve action was picked up from a sync's `actions`) -- this is what
        a subsequent sync()'s `requests` echo needs to carry for the relay to flip its display
        status, per host_sync()'s docstring."""
        req = self.requests[request_id]
        req["status"] = "approved"
        req["approved_at"] = time.time()
        self.up_next.append(dict(req))

    def mark_played(self, request_id: str) -> None:
        req = self.requests[request_id]
        req["status"] = "played"
        req["resolved_at"] = time.time()
        self.up_next = [e for e in self.up_next if e["id"] != request_id]
