"""
End-to-end request-lifecycle integration tests: a simulated kiosk (FakeHost) registers with
the relay, a "remote customer" submits a request through the real customer-facing API, a
"remote admin" reads/acts on it through the real bartender-facing API, and the kiosk's next
sync tick round-trips the result back out -- the same three-way flow customer.html /
admin.html / a real host app go through, minus an actual browser or mobile build.

See CLAUDE.md's "host is source of truth; host broadcasts its own state" note -- these tests
exist to catch a regression in that flow specifically, not to be a general relay test suite.
"""

from __future__ import annotations

from fake_host import FakeHost

CATALOG = [
    {"id": "song-1", "artist": "Test Artist", "title": "Song One", "album": "Album", "duration_seconds": 180},
    {"id": "song-2", "artist": "Test Artist", "title": "Song Two", "album": "Album", "duration_seconds": 200},
]


def test_free_auto_accept_request_reaches_admin_and_plays(client):
    """Free/auto-accept mode (both stripe_enabled and bartender_enabled off): a customer
    request needs no human review at all, so it should display as "approved" on the admin
    Requests/Up Next view immediately -- even before the kiosk's own echo confirms it -- per
    bartender_requests()'s computed-display-status override."""
    host = FakeHost(client)
    host.register(CATALOG, pin_hash="admin-pin-hash", bartender_enabled=False)

    # Remote customer page submits a free request.
    resp = client.post(
        f"/api/bar/{host.jukebar_id}/request",
        params={"s": host.session},
        json={"song_ids": ["song-1"], "song_titles": ["Song One"], "requester_name": "Alice"},
    )
    assert resp.status_code == 200
    request_id = resp.json()["request_id"]
    assert resp.json()["status"] == "pending"  # relay is never the authority on final status

    # Remote admin authenticates and lists requests -- should already show "approved" display
    # status, no tap needed, even though the underlying stored status is still "pending".
    auth = client.post(f"/api/bar/{host.jukebar_id}/authenticate", params={"s": host.session},
                        json={"pin_hash": "admin-pin-hash", "role": "admin"})
    assert auth.status_code == 200
    token = auth.json()["token"]

    listed = client.get(f"/api/bar/{host.jukebar_id}/requests",
                         params={"s": host.session, "token": token}).json()
    assert len(listed["requests"]) == 1
    assert listed["requests"][0]["id"] == request_id
    assert listed["requests"][0]["status"] == "approved"
    assert listed["requests"][0]["requester_name"] == "Alice"

    # Kiosk's next sync tick picks up the new request via new_requests (status == "pending"
    # server-side, regardless of the display override above).
    sync_result = host.sync()
    assert any(r["id"] == request_id for r in sync_result["requests"])

    # Kiosk injects it into its live queue and confirms via its own requests echo.
    host.approve_and_inject(request_id)
    host.sync()

    # A second sync's new_requests must NOT include it again -- it's no longer "pending"
    # server-side once the host's echo updated it, so it shouldn't be re-offered as new.
    sync_result_2 = host.sync()
    assert not any(r["id"] == request_id for r in sync_result_2["requests"])

    # Kiosk eventually reports it played; echoed on the next sync.
    host.mark_played(request_id)
    host.sync()

    # Admin's Requests/Up Next view no longer lists it (only pending/approved/approved_jump do).
    listed_after = client.get(f"/api/bar/{host.jukebar_id}/requests",
                               params={"s": host.session, "token": token}).json()
    assert not any(r["id"] == request_id for r in listed_after["requests"])

    # But it's still in history, correctly marked played.
    history = client.get(f"/api/bar/{host.jukebar_id}/history",
                          params={"s": host.session, "token": token}).json()
    played = next(r for r in history["requests"] if r["id"] == request_id)
    assert played["status"] == "played"


def test_bartender_pay_request_needs_explicit_approval(client):
    """Pay-to-bartender mode (bartender_enabled=True, stripe off): a request needs an
    explicit admin/bartender approve tap before the kiosk should ever see it as approved --
    unlike the auto-accept case above, it must show as "pending" (needs review) on admin's
    view, and the approve action must round-trip through pending_actions to the kiosk."""
    host = FakeHost(client)
    host.register(CATALOG, pin_hash="admin-pin-hash", bartender_enabled=True,
                  price_per_song=3.0)

    resp = client.post(
        f"/api/bar/{host.jukebar_id}/request",
        params={"s": host.session},
        json={"song_ids": ["song-2"], "song_titles": ["Song Two"], "requester_name": "Bob"},
    )
    request_id = resp.json()["request_id"]
    assert resp.json()["status"] == "pending"

    auth = client.post(f"/api/bar/{host.jukebar_id}/authenticate", params={"s": host.session},
                        json={"pin_hash": "admin-pin-hash", "role": "admin"})
    token = auth.json()["token"]

    # Still needs review -- must NOT be display-overridden to "approved" the way free/
    # auto-accept was in the test above, since bartender_enabled makes require_approval True.
    listed = client.get(f"/api/bar/{host.jukebar_id}/requests",
                         params={"s": host.session, "token": token}).json()
    assert listed["requests"][0]["status"] == "pending"
    assert listed["requests"][0]["price"] == 3.0

    # Admin approves.
    approve = client.post(f"/api/bar/{host.jukebar_id}/approve",
                           params={"s": host.session, "token": token},
                           json={"request_id": request_id, "jump": False})
    assert approve.status_code == 200

    # A bartender manually approving a pending request IS the payment confirmation --
    # confirm the relay marked it paid via the bartender path, not left unpaid.
    listed_after_approve = client.get(f"/api/bar/{host.jukebar_id}/requests",
                                       params={"s": host.session, "token": token}).json()
    approved_req = listed_after_approve["requests"][0]
    assert approved_req["paid"] is True
    assert approved_req["payment_method"] == "bartender"

    # Kiosk's next sync picks up the approve action from pending_actions -- note the request
    # itself also arrives via new_requests in this same response, since bartender_approve()
    # deliberately never touches the underlying stored status ("Don't mark approved here --
    # host confirms via up_next on next sync"), only pending_actions. FakeHost.sync() auto-
    # adopts it into host.requests, same as the free/auto-accept flow above.
    sync_result = host.sync()
    approve_actions = [a for a in sync_result["actions"] if a["type"] == "approve"]
    assert len(approve_actions) == 1
    assert approve_actions[0]["request_id"] == request_id
    assert request_id in host.requests

    # Kiosk acts on the approve action, echoes the result back.
    host.approve_and_inject(request_id)
    host.sync()

    host.mark_played(request_id)
    host.sync()

    history = client.get(f"/api/bar/{host.jukebar_id}/history",
                          params={"s": host.session, "token": token}).json()
    played = next(r for r in history["requests"] if r["id"] == request_id)
    assert played["status"] == "played"
    assert played["payment_method"] == "bartender"
