"""
JukeBar web — FastAPI relay backend on Render.

Map / directory (any connection mode — called once at setup):
  POST /api/map/register           — register or refresh a bar's map entry (artist names only)
  GET  /api/map                    — list all registered bars; is_live=true for internet-mode bars

Host (iOS, internet mode only):
  POST /api/host/register          — startup registration with full catalog + session
  POST /api/host/sync              — every 5 s: push now_playing_id + played IDs, get pending requests + actions

Customer endpoints (internet mode, session required via ?s=):
  GET  /api/bar/{id}/catalog       — browse full catalog
  GET  /api/bar/{id}/nowplaying    — current track
  POST /api/bar/{id}/request       — submit song request
  GET  /api/bar/{id}/request/{rid} — check request status

Bartender endpoints (internet mode, session required via ?s=):
  GET  /api/bar/{id}/requests      — list pending requests
  POST /api/bar/{id}/approve       — approve a request (jump flag supported)
  POST /api/bar/{id}/deny          — deny a request
"""
import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
MAP_ENTRIES_FILE = DATA_DIR / "map_entries.json"

BAR_TIMEOUT_SECONDS  = 300    # bar shown as offline on the map after 5 min without a sync
BAR_CLEANUP_INACTIVE = 7200   # last_seen must be this old (2 h) before cleanup considers it
BAR_CLEANUP_MIN_AGE  = 1800   # session must also be at least this old (30 min) to be swept
CLEANUP_INTERVAL     = 300    # sweep runs every 5 minutes


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class MapEntry:
    """
    Lightweight directory record — any connection mode, persisted to disk.
    Stores artist names only, not the full song catalog.
    """
    jukebar_id: str
    bar_name: str
    location: str       # human-readable venue address or city
    artists: list[str]  # sorted, deduplicated artist names from the playlist
    registered_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)


@dataclass
class SongRequest:
    id: str
    song_ids: list[str]
    song_titles: list[str]
    requester_name: str
    customer_id: str
    jump: bool
    status: str  # "pending" | "approved" | "denied" | "played"
    created_at: float = field(default_factory=time.time)


@dataclass
class BarSession:
    """
    Full relay session — internet mode only, held in memory.
    iOS re-registers within 5 s of any restart so memory-only is fine here.
    The full catalog is kept here for customer browsing; it is NOT stored on disk.
    """
    jukebar_id: str
    session: str          # playlist_id from iOS — rotates on every app restart
    bar_name: str
    require_approval: bool
    catalog: list[dict]        # full song objects for /api/bar/{id}/catalog
    song_index: dict = field(default_factory=dict)  # id → song dict, built at register time
    price_per_song: float = 0.0
    price_for_three: float = 0.0
    currency: str = ""
    now_playing: dict | None = None    # full song dict pushed by iOS on song change
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    requests: dict[str, SongRequest] = field(default_factory=dict)
    pending_actions: list[dict] = field(default_factory=list)
    pin_hash: str = ""  # SHA-256 hex of admin PIN — set at register; required for bartender web auth


_map_entries: dict[str, MapEntry] = {}   # persisted to disk
_bars: dict[str, BarSession] = {}        # in-memory only


# ---------------------------------------------------------------------------
# Disk persistence for map entries
# ---------------------------------------------------------------------------

def _load_map_entries() -> dict[str, MapEntry]:
    if not MAP_ENTRIES_FILE.exists():
        return {}
    try:
        raw = json.loads(MAP_ENTRIES_FILE.read_text(encoding="utf-8"))
        return {jid: MapEntry(**entry) for jid, entry in raw.items()}
    except Exception:
        return {}


def _write_map_entries_sync() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MAP_ENTRIES_FILE.write_text(
        json.dumps({jid: asdict(e) for jid, e in _map_entries.items()}, indent=2),
        encoding="utf-8",
    )


async def _save_map_entries() -> None:
    await asyncio.to_thread(_write_map_entries_sync)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def _cleanup_loop():
    """Purge BarSessions that haven't synced in BAR_CLEANUP_INACTIVE and are older than BAR_CLEANUP_MIN_AGE."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now = time.time()
        stale = [
            jid for jid, bar in _bars.items()
            if (now - bar.last_seen  > BAR_CLEANUP_INACTIVE and
                now - bar.created_at > BAR_CLEANUP_MIN_AGE)
        ]
        for jid in stale:
            del _bars[jid]
        if stale:
            print(f"[cleanup] purged {len(stale)} stale session(s): {stale}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _map_entries
    _map_entries = _load_map_entries()
    asyncio.create_task(_cleanup_loop())
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Static / health
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return (Path("static") / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_bar(jukebar_id: str) -> BarSession:
    bar = _bars.get(jukebar_id)
    if bar is None:
        raise HTTPException(404, "JukeBar not found or offline")
    return bar


def _validate_session(bar: BarSession, session: str) -> None:
    if bar.session != session:
        raise HTTPException(403, "Invalid session")


def _touch(bar: BarSession) -> None:
    bar.last_seen = time.time()
    if bar.jukebar_id in _map_entries:
        _map_entries[bar.jukebar_id].last_seen = time.time()


# ---------------------------------------------------------------------------
# Map / directory (any connection mode)
# ---------------------------------------------------------------------------

@app.post("/api/map/register")
async def map_register(body: dict[str, Any]):
    """
    Called by iOS at setup time regardless of connection mode.
    Stores only artist names — not full song data.
    Persists to disk immediately so entries survive Render restarts.
    """
    jukebar_id = body.get("jukebar_id", "")
    if not jukebar_id:
        raise HTTPException(400, "jukebar_id required")

    existing = _map_entries.get(jukebar_id)
    _map_entries[jukebar_id] = MapEntry(
        jukebar_id=jukebar_id,
        bar_name=body.get("bar_name", ""),
        location=body.get("location", ""),
        artists=sorted(body.get("artists", [])),
        registered_at=existing.registered_at if existing else time.time(),
    )
    await _save_map_entries()
    return {"ok": True}


@app.get("/api/map")
async def map_bars():
    """
    All registered bars with their artist lists.
    is_live=true only for internet-mode bars that have polled within 5 minutes.
    """
    cutoff = time.time() - BAR_TIMEOUT_SECONDS
    result = [
        {
            "jukebar_id": e.jukebar_id,
            "bar_name": e.bar_name,
            "location": e.location,
            "artists": e.artists,
            "registered_at": e.registered_at,
            "last_seen": e.last_seen,
            "is_live": e.last_seen >= cutoff and e.jukebar_id in _bars,
        }
        for e in _map_entries.values()
    ]
    return {"bars": result}


# ---------------------------------------------------------------------------
# Host endpoints (iOS, internet mode only)
# ---------------------------------------------------------------------------

@app.post("/api/host/register")
async def host_register(body: dict[str, Any]):
    """
    Called by iOS on startup when running in internet relay mode.
    Accepts the full catalog for customer browsing — held in memory only.
    """
    jukebar_id = body.get("jukebar_id", "")
    session = body.get("session", "")
    if not jukebar_id or not session:
        raise HTTPException(400, "jukebar_id and session required")

    catalog = body.get("catalog", [])
    _bars[jukebar_id] = BarSession(
        jukebar_id=jukebar_id,
        session=session,
        bar_name=body.get("bar_name", ""),
        require_approval=body.get("require_approval", True),
        catalog=catalog,
        song_index={s["id"]: s for s in catalog if "id" in s},
        price_per_song=body.get("price_per_song", 0.0),
        price_for_three=body.get("price_for_three", 0.0),
        currency=body.get("currency", ""),
        now_playing=body.get("now_playing"),
        pin_hash=body.get("pin_hash", ""),
    )
    # Keep map entry's last_seen fresh if the bar is registered there
    if jukebar_id in _map_entries:
        _map_entries[jukebar_id].last_seen = time.time()
    return {"ok": True}


@app.post("/api/host/nowplaying")
async def host_nowplaying(body: dict[str, Any]):
    """
    Called by iOS immediately when the now-playing item changes.
    Lightweight alternative to waiting for the next /sync cycle.
    """
    jukebar_id = body.get("jukebar_id", "")
    session    = body.get("session", "")
    bar = _get_bar(jukebar_id)
    _validate_session(bar, session)
    _touch(bar)
    bar.now_playing = body.get("now_playing")  # full song dict or null
    return {"ok": True}


@app.post("/api/host/sync")
async def host_sync(body: dict[str, Any]):
    """
    Called by iOS every 5 s — replaces separate poll + update calls.

    iOS sends:
      now_playing_id    — persistent song ID of the currently playing track (or null)
      played_request_ids — request IDs whose last song just started playing

    Server returns:
      requests — new customer requests since last sync (status == "pending")
      actions  — queued bartender approve/deny actions, then clears the queue
    """
    jukebar_id = body.get("jukebar_id", "")
    session    = body.get("session", "")
    bar = _get_bar(jukebar_id)
    _validate_session(bar, session)
    _touch(bar)

    for rid in body.get("played_request_ids", []):
        if rid in bar.requests:
            bar.requests[rid].status = "played"

    new_requests = [
        {
            "id": r.id,
            "song_ids": r.song_ids,
            "song_titles": r.song_titles,
            "requester_name": r.requester_name,
            "jump": r.jump,
        }
        for r in bar.requests.values()
        if r.status == "pending"
    ]

    actions = list(bar.pending_actions)
    bar.pending_actions.clear()

    return {"requests": new_requests, "actions": actions}


# ---------------------------------------------------------------------------
# Customer endpoints (internet mode)
# ---------------------------------------------------------------------------

def _customer_bar(jukebar_id: str, session: str) -> BarSession:
    bar = _get_bar(jukebar_id)
    _validate_session(bar, session)
    _touch(bar)
    return bar


_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/bar/{jukebar_id}", response_class=HTMLResponse)
async def bar_page(jukebar_id: str):
    return HTMLResponse((Path("static") / "customer.html").read_text(encoding="utf-8"), headers=_NO_CACHE)


@app.get("/bartender/{jukebar_id}", response_class=HTMLResponse)
async def bartender_page(jukebar_id: str):
    return HTMLResponse((Path("static") / "bartender.html").read_text(encoding="utf-8"), headers=_NO_CACHE)


@app.get("/admin/{jukebar_id}", response_class=HTMLResponse)
async def admin_page(jukebar_id: str):
    return HTMLResponse((Path("static") / "admin.html").read_text(encoding="utf-8"), headers=_NO_CACHE)


@app.get("/api/bar/{jukebar_id}/catalog")
async def bar_catalog(jukebar_id: str, s: str = Query(..., alias="s")):
    bar = _customer_bar(jukebar_id, s)
    return JSONResponse(
        {
            "bar_name":        bar.bar_name,
            "catalog":         bar.catalog,
            "require_approval": bar.require_approval,
            "price_per_song":  bar.price_per_song,
            "price_for_three": bar.price_for_three,
            "currency":        bar.currency,
        },
        headers={"Cache-Control": "max-age=300, private"},
    )


@app.post("/api/bar/{jukebar_id}/authenticate")
async def bar_authenticate(jukebar_id: str, s: str = Query(..., alias="s"), body: dict[str, Any] = ...):
    """
    Bartender PIN gate. Client hashes the PIN with SHA-256 (same algorithm as iOS CryptoKit)
    and posts the hex digest. Returns 200 OK or 403 Forbidden.
    If the bar has no pin_hash (legacy / not yet set), access is denied — PIN is mandatory.
    """
    bar = _customer_bar(jukebar_id, s)
    if not bar.pin_hash:
        raise HTTPException(403, "No PIN configured for this bar")
    if body.get("pin_hash") != bar.pin_hash:
        raise HTTPException(403, "Incorrect PIN")
    return {"ok": True}


@app.get("/api/bar/{jukebar_id}/nowplaying")
async def bar_nowplaying(jukebar_id: str, s: str = Query(..., alias="s")):
    bar = _customer_bar(jukebar_id, s)
    return {"now_playing": bar.now_playing}


@app.post("/api/bar/{jukebar_id}/request")
async def bar_request(jukebar_id: str, s: str = Query(..., alias="s"), body: dict[str, Any] = ...):
    bar = _customer_bar(jukebar_id, s)

    song_ids: list[str] = body.get("song_ids", [])
    if not song_ids:
        raise HTTPException(400, "song_ids required")

    rid = str(uuid.uuid4())
    req = SongRequest(
        id=rid,
        song_ids=song_ids,
        song_titles=body.get("song_titles", []),
        requester_name=body.get("requester_name", ""),
        customer_id=body.get("customer_id", ""),
        jump=body.get("jump", False),
        status="pending" if bar.require_approval else "approved",
    )
    bar.requests[rid] = req

    if not bar.require_approval:
        bar.pending_actions.append({
            "type": "approve",
            "request_id": rid,
            "song_ids": song_ids,
            "jump": req.jump,
        })

    return {"request_id": rid, "status": req.status}


@app.get("/api/bar/{jukebar_id}/request/{request_id}")
async def bar_request_status(jukebar_id: str, request_id: str, s: str = Query(..., alias="s")):
    bar = _customer_bar(jukebar_id, s)
    req = bar.requests.get(request_id)
    if req is None:
        raise HTTPException(404, "Request not found")
    return {"status": req.status, "song_titles": req.song_titles}


# ---------------------------------------------------------------------------
# Bartender endpoints (internet mode)
# ---------------------------------------------------------------------------

@app.get("/api/bar/{jukebar_id}/requests")
async def bartender_requests(jukebar_id: str, s: str = Query(..., alias="s")):
    bar = _customer_bar(jukebar_id, s)
    pending = [
        {
            "id": r.id,
            "song_ids": r.song_ids,
            "song_titles": r.song_titles,
            "song_details": [
                {
                    "artist": bar.song_index.get(sid, {}).get("artist", ""),
                    "album":  bar.song_index.get(sid, {}).get("album", ""),
                    "title":  bar.song_index.get(sid, {}).get("title", ""),
                }
                for sid in r.song_ids
            ],
            "requester_name": r.requester_name,
            "customer_id": r.customer_id,
            "jump": r.jump,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in sorted(bar.requests.values(), key=lambda r: r.created_at)
        if r.status in ("pending", "approved")
    ]
    return {"bar_name": bar.bar_name, "requests": pending, "now_playing": bar.now_playing,
            "price_per_song": bar.price_per_song, "price_for_three": bar.price_for_three,
            "currency": bar.currency}


@app.post("/api/bar/{jukebar_id}/approve")
async def bartender_approve(jukebar_id: str, s: str = Query(..., alias="s"), body: dict[str, Any] = ...):
    bar = _customer_bar(jukebar_id, s)
    rid = body.get("request_id", "")
    req = bar.requests.get(rid)
    if req is None:
        raise HTTPException(404, "Request not found")
    jump = body.get("jump", req.jump)
    req.status = "approved"
    req.jump = jump
    bar.pending_actions.append({
        "type": "approve",
        "request_id": rid,
        "song_ids": req.song_ids,
        "jump": jump,
    })
    return {"ok": True}


@app.get("/api/bar/{jukebar_id}/history")
async def bar_history(jukebar_id: str, s: str = Query(..., alias="s")):
    """All requests for the current session — used by admin page reports tab."""
    bar = _customer_bar(jukebar_id, s)
    return {
        "bar_name": bar.bar_name,
        "requests": [
            {
                "id": r.id,
                "song_titles": r.song_titles,
                "requester_name": r.requester_name,
                "status": r.status,
                "jump": r.jump,
                "created_at": r.created_at,
            }
            for r in sorted(bar.requests.values(), key=lambda r: r.created_at)
        ],
    }


@app.post("/api/bar/{jukebar_id}/control")
async def bartender_control(jukebar_id: str, s: str = Query(..., alias="s"), body: dict[str, Any] = ...):
    bar = _customer_bar(jukebar_id, s)
    action = body.get("action", "")
    if action not in ("play", "pause", "next", "prev"):
        raise HTTPException(400, "action must be play, pause, next, or prev")
    bar.pending_actions.append({"type": "control", "action": action})
    return {"ok": True}


@app.post("/api/bar/{jukebar_id}/deny")
async def bartender_deny(jukebar_id: str, s: str = Query(..., alias="s"), body: dict[str, Any] = ...):
    bar = _customer_bar(jukebar_id, s)
    rid = body.get("request_id", "")
    req = bar.requests.get(rid)
    if req is None:
        raise HTTPException(404, "Request not found")
    req.status = "denied"
    bar.pending_actions.append({
        "type": "deny",
        "request_id": rid,
    })
    return {"ok": True}
