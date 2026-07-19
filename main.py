"""
/Users/laszlo/PycharmProjects/jukebarweb/main.py

RESPONSIBILITY: Single-file FastAPI app — the relay between iOS/Android hosts
  and internet-mode customers/admins/bartenders, plus the public jukebars.com
  community pages (index, discover). Holds all bar state in memory only
  (BarSession); nothing here survives a restart except what's mirrored to GCS
  via gcs_store.py.
CALLED BY: Hosts (AppState.swift / RelayService.kt) over /api/host/*; browsers
  over /api/bar/{id}/* and /admin, /bartender, / (Render serves this directly
  as jukebars.com). Never called by profile_daemon.py — that process only
  ever shares state with this one through GCS.
KEY METHODS:
  - host_register() / host_sync() — full re-register vs. the 5s heartbeat;
    business logic for what a stale bar looks like lives in _cleanup_loop()
  - bar_request() / bar_create_payment_intent() / bar_payment_confirmed() —
    the accepting_requests gate lives in the first two only, never the third
  - map_register() / _load_bar_profiles_sync() — the map/discovery + genre
    round-trip through GCS (see docs/architecture.html, flow E)
  - bar_settings() / host_sync() — settings propagation: bar_settings() only
    writes into BarSession.desired_settings (latest write per field wins, no
    queue). host_sync() unconditionally trusts the host's own echoed current
    values (no version/ordering guard - the host re-sends them every 5s
    regardless, so any single dropped/reordered call self-heals on the next
    tick) and clears a desired_settings entry once the echo matches it.
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
MAP_ENTRIES_FILE = DATA_DIR / "map_entries.json"

_USE_GCS = bool(os.environ.get("GCS_BUCKET"))

BAR_TIMEOUT_SECONDS  = 300    # bar shown as offline on the map after 5 min without a sync
BAR_CLEANUP_INACTIVE = 7200   # last_seen must be this old (2 h) before cleanup considers it
BAR_CLEANUP_MIN_AGE  = 1800   # session must also be at least this old (30 min) to be swept
CLEANUP_INTERVAL     = 300    # sweep runs every 5 minutes

PROFILING_ON      = os.environ.get("PROFILING_ON", "false").lower() == "true"
PROFILE_CACHE_TTL = 300.0   # seconds between GCS profile re-reads


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class MapEntry:
    """
    Lightweight directory record — any connection mode, persisted to disk.
    Stores up to 3 playlists (name + artist list), kept by most-recently-updated order.
    """
    jukebar_id: str
    bar_name: str
    location: str       # human-readable venue address or city
    playlists: list[dict] = field(default_factory=list)  # [{name, artists, updated_at}]
    lat: float | None = None
    lng: float | None = None
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
    approved_at: float = 0.0
    paid: bool = False
    # "free" (auto-accepted, no payment) | "bartender" (cash/card collected in person, bartender
    # approval IS the payment confirmation) | "stripe" (paid online). paid is true for both
    # bartender and stripe - only "free" means no payment happened at all.
    payment_method: str = "free"


@dataclass
class BarSession:
    """
    main.py, class BarSession

    RESPONSIBILITY: One bar's entire live state — catalog, settings, pending
      requests, up-next queue — held in memory only, keyed by jukebar_id in
      the module-level _bars dict. host_register() updates an existing entry
      in place when it's the same ongoing session, only building fresh for a
      genuinely new one; if jukebarweb restarts, a bar just looks briefly
      offline until the host's next sync.
    CALLED BY: Every /api/bar/{id}/* and /api/host/* handler in this file.
    KEY METHODS: require_approval (property) — derives approval mode from
      stripe_enabled/bartender_enabled rather than storing a separate flag.
    """
    jukebar_id: str
    session: str          # playlist_id from iOS — rotates on every app restart
    bar_name: str
    catalog: list[dict]        # full song objects for /api/bar/{id}/catalog
    bartender_enabled: bool = True  # false = hide Submit to Bartender on customer page
    accepting_requests: bool = True  # false = hide Request/Pay buttons + QR everywhere; admin/bartender still work
    song_index: dict = field(default_factory=dict)  # id → song dict, built at register time
    price_per_song: float = 0.0
    price_for_three: float = 0.0
    currency: str = ""
    playlist_name: str = ""
    stripe_enabled: bool = False       # false = send empty key to customer (even if key is stored)
    stripe_publishable_key: str = ""
    stripe_secret_key: str = ""
    now_playing: dict | None = None    # full song dict pushed by host on song change
    is_playing: bool = True            # pushed by host on play/pause; inferred True by default for iOS compat
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    requests: dict[str, SongRequest] = field(default_factory=dict)
    pending_actions: list[dict] = field(default_factory=list)
    up_next_queue: list[dict] = field(default_factory=list)  # authoritative host queue, pushed every sync
    pin_hash: str = ""  # SHA-256 hex of admin PIN — set at register; required for bartender web auth
    # field -> value an admin/bartender asked for that the host hasn't echoed back yet;
    # presence = pending. Latest write per field wins, no queue — see bar_settings()/host_sync().
    desired_settings: dict[str, bool] = field(default_factory=dict)

    @property
    def require_approval(self) -> bool:
        return self.stripe_enabled or self.bartender_enabled


_map_entries: dict[str, MapEntry] = {}   # persisted to disk
_bars: dict[str, BarSession] = {}        # in-memory only
_bar_profiles: dict[str, dict] = {}     # bar_id → profile.json contents; refreshed from GCS every PROFILE_CACHE_TTL s
_profiles_loaded_at: float = 0.0


# ---------------------------------------------------------------------------
# Disk persistence for map entries
# ---------------------------------------------------------------------------

def _load_map_entries() -> dict[str, MapEntry]:
    try:
        if _USE_GCS:
            import gcs_store
            raw_text = gcs_store.read("map_entries.json")
        else:
            if not MAP_ENTRIES_FILE.exists():
                return {}
            raw_text = MAP_ENTRIES_FILE.read_text(encoding="utf-8")
        if not raw_text:
            return {}
        raw = json.loads(raw_text)
        result = {}
        for jid, entry in raw.items():
            fields = {k: v for k, v in entry.items() if k in MapEntry.__dataclass_fields__}
            # migrate old flat artists list to playlists format
            if "artists" in entry and not fields.get("playlists"):
                fields["playlists"] = [{
                    "name": "Playlist",
                    "artists": entry["artists"],
                    "updated_at": entry.get("last_seen", time.time()),
                }]
            result[jid] = MapEntry(**fields)
        return result
    except Exception:
        return {}


def _write_map_entries_sync() -> None:
    content = json.dumps({jid: asdict(e) for jid, e in _map_entries.items()}, indent=2)
    try:
        if _USE_GCS:
            import gcs_store
            gcs_store.write("map_entries.json", content)
            print(f"[map] wrote {len(_map_entries)} entries to GCS")
        else:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            MAP_ENTRIES_FILE.write_text(content, encoding="utf-8")
            print(f"[map] wrote {len(_map_entries)} entries to {MAP_ENTRIES_FILE}")
    except Exception as e:
        print(f"[map] ERROR writing map entries: {e}")


async def _save_map_entries() -> None:
    await asyncio.to_thread(_write_map_entries_sync)


def _load_bar_profiles_sync() -> None:
    global _bar_profiles, _profiles_loaded_at
    if not _USE_GCS:
        return
    import gcs_store
    new_profiles = {}
    for bar_id in _map_entries:
        raw = gcs_store.read(f"map/{bar_id}/profile.json")
        if raw:
            try:
                new_profiles[bar_id] = json.loads(raw)
            except Exception:
                pass
    _bar_profiles = new_profiles
    _profiles_loaded_at = time.time()
    print(f"[profiles] loaded {len(_bar_profiles)} bar profile(s) from GCS")


async def _maybe_refresh_profiles() -> None:
    if time.time() - _profiles_loaded_at > PROFILE_CACHE_TTL:
        await asyncio.to_thread(_load_bar_profiles_sync)


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
    print(f"[startup] DATA_DIR={DATA_DIR} USE_GCS={_USE_GCS}")
    print(f"[startup] map file path: {MAP_ENTRIES_FILE}")
    print(f"[startup] map file exists: {MAP_ENTRIES_FILE.exists()}")
    if MAP_ENTRIES_FILE.exists():
        print(f"[startup] map file size: {MAP_ENTRIES_FILE.stat().st_size} bytes")
    _map_entries = _load_map_entries()
    print(f"[startup] loaded {len(_map_entries)} map entries: {list(_map_entries.keys())}")
    _load_bar_profiles_sync()
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


@app.get("/ios", response_class=HTMLResponse)
async def ios_docs():
    return (Path("static") / "ios.html").read_text(encoding="utf-8")


@app.get("/android", response_class=HTMLResponse)
async def android_docs():
    return (Path("static") / "android.html").read_text(encoding="utf-8")


@app.get("/discover", response_class=HTMLResponse)
async def discover():
    return HTMLResponse((Path("static") / "discover.html").read_text(encoding="utf-8"), headers=_NO_CACHE)


@app.get("/youwouldnotguesss/architecture", response_class=HTMLResponse)
async def internal_architecture_doc():
    # Unlisted on purpose — not linked from any page. Keep this path out of
    # sitemaps/nav; the only distribution channel is sharing the URL directly.
    # /youwouldnotguesss/ is the namespace for internal-only docs like this
    # one — add more pages under it the same way rather than inventing a new
    # random slug each time.
    headers = {**_NO_CACHE, "X-Robots-Tag": "noindex, nofollow"}
    return HTMLResponse((Path("docs") / "architecture.html").read_text(encoding="utf-8"), headers=headers)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/.well-known/apple-developer-merchantid-domain-association")
async def apple_pay_domain_verification():
    p = Path("static/.well-known/apple-developer-merchantid-domain-association")
    if not p.exists():
        raise HTTPException(404, "Apple Pay domain verification file not configured")
    return FileResponse(p, media_type="application/octet-stream")


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
    Keeps up to 3 playlists per bar (by playlist_name); same name refreshes, new name appends.
    Persists to disk immediately so entries survive Render restarts.
    """
    jukebar_id = body.get("jukebar_id", "")
    if not jukebar_id:
        raise HTTPException(400, "jukebar_id required")

    playlist_name         = body.get("playlist_name") or "Playlist"
    playlist_display_name = body.get("playlist_display_name") or playlist_name
    playlist_note         = body.get("playlist_note", "")
    raw_artists = body.get("artists", [])
    if raw_artists and isinstance(raw_artists[0], str):
        # legacy format: ["Artist Name", ...]
        new_artists = sorted(
            [{"name": a, "song_count": 1} for a in raw_artists if isinstance(a, str)],
            key=lambda x: x["name"].casefold(),
        )
    else:
        # new format: [{"name": ..., "song_count": N}, ...]
        new_artists = sorted(
            [a for a in raw_artists if isinstance(a, dict) and a.get("name")],
            key=lambda x: x["name"].casefold(),
        )
    now           = time.time()
    existing      = _map_entries.get(jukebar_id)

    playlists = list(existing.playlists) if existing else []
    updated = False
    for p in playlists:
        if p["name"] == playlist_name:
            p["display_name"] = playlist_display_name
            p["artists"]      = new_artists
            p["note"]         = playlist_note
            p["updated_at"]   = now
            updated = True
            break
    if not updated:
        playlists.append({"name": playlist_name, "display_name": playlist_display_name,
                          "note": playlist_note, "artists": new_artists, "updated_at": now})

    # keep the 3 most recently updated
    playlists.sort(key=lambda p: p["updated_at"], reverse=True)
    playlists = playlists[:3]

    lat = body.get("lat")
    lng = body.get("lng")

    _map_entries[jukebar_id] = MapEntry(
        jukebar_id=jukebar_id,
        bar_name=body.get("bar_name", ""),
        location=body.get("location", ""),
        playlists=playlists,
        lat=float(lat) if lat is not None else (existing.lat if existing else None),
        lng=float(lng) if lng is not None else (existing.lng if existing else None),
        registered_at=existing.registered_at if existing else now,
    )
    await _save_map_entries()
    return {"ok": True}


@app.post("/api/map/unregister")
async def map_unregister(body: dict[str, Any]):
    """Called by iOS when listOnMap is turned off. Wipes all playlist data for this bar."""
    jukebar_id = body.get("jukebar_id", "")
    if jukebar_id and jukebar_id in _map_entries:
        del _map_entries[jukebar_id]
        await _save_map_entries()
    return {"ok": True}


@app.get("/api/map")
async def map_bars():
    """
    All registered bars with their playlists and genre profiling data.
    is_live=true only for internet-mode bars that have polled within 5 minutes.
    profiling_on=true means discover.html should use genre colors.
    """
    await _maybe_refresh_profiles()
    cutoff = time.time() - BAR_TIMEOUT_SECONDS
    result = []
    for e in _map_entries.values():
        raw_profile = _bar_profiles.get(e.jukebar_id)
        profiling = None
        if raw_profile:
            artist_colors: dict[str, str] = {}
            artist_tags: dict[str, list] = {}
            for pl_data in raw_profile.get("playlists", {}).values():
                for a in pl_data.get("artists", []):
                    name = a.get("name")
                    color = a.get("band_color")
                    tags = a.get("tags")
                    if name and color:
                        artist_colors[name] = color
                    if name and tags:
                        artist_tags[name] = tags
            profiling = {
                "combined_pie":  raw_profile.get("combined_pie", {}),
                "artist_colors": artist_colors,
                "artist_tags":   artist_tags,
            }
        result.append({
            "jukebar_id":    e.jukebar_id,
            "bar_name":      e.bar_name,
            "location":      e.location,
            "lat":           e.lat,
            "lng":           e.lng,
            "playlists":     [
                {**p, "artists": [
                    a["name"] if isinstance(a, dict) else a
                    for a in p.get("artists", [])
                ]}
                for p in e.playlists
            ],
            "registered_at": e.registered_at,
            "last_seen":     e.last_seen,
            "is_live":       e.last_seen >= cutoff and e.jukebar_id in _bars,
            "profiling":     profiling,
        })
    return {"bars": result, "profiling_on": PROFILING_ON}


# ---------------------------------------------------------------------------
# Host endpoints (iOS, internet mode only)
# ---------------------------------------------------------------------------

@app.post("/api/host/register")
async def host_register(body: dict[str, Any]):
    """
    Called by iOS/Android on startup when running in internet relay mode, and again on every
    live settings change (payment toggles, accepting_requests) to push the new values - the
    host has no lighter-weight "just update settings" call, so it reuses full register.

    For that reason, re-registering the SAME ongoing session (jukebar_id + session both match
    an existing entry) updates fields in place rather than replacing the BarSession outright -
    otherwise now_playing/is_playing/up_next_queue/pending requests (none of which the register
    payload carries) would go blank on every settings toggle until the next natural song change
    or sync. A different (or missing) session means a genuine new session - build fresh so state
    from the previous session doesn't leak in.
    """
    jukebar_id = body.get("jukebar_id", "")
    session = body.get("session", "")
    if not jukebar_id or not session:
        raise HTTPException(400, "jukebar_id and session required")

    catalog = body.get("catalog", [])
    pk = body.get("stripe_publishable_key", "")
    existing = _bars.get(jukebar_id)

    if existing is not None and existing.session == session:
        bar = existing
        bar.bar_name = body.get("bar_name", "")
        bar.playlist_name = body.get("playlist_name", "")
        bar.bartender_enabled = body.get("bartender_enabled", True)
        bar.accepting_requests = body.get("accepting_requests", True)
        bar.catalog = catalog
        bar.song_index = {s["id"]: s for s in catalog if "id" in s}
        bar.price_per_song = body.get("price_per_song", 0.0)
        bar.price_for_three = body.get("price_for_three", 0.0)
        bar.currency = body.get("currency", "")
        bar.stripe_enabled = body.get("stripe_enabled", bool(pk))
        bar.stripe_publishable_key = pk
        bar.stripe_secret_key = body.get("stripe_secret_key", "")
        bar.pin_hash = body.get("pin_hash", "")
        bar.last_seen = time.time()
    else:
        _bars[jukebar_id] = BarSession(
            jukebar_id=jukebar_id,
            session=session,
            bar_name=body.get("bar_name", ""),
            playlist_name=body.get("playlist_name", ""),
            bartender_enabled=body.get("bartender_enabled", True),
            accepting_requests=body.get("accepting_requests", True),
            catalog=catalog,
            song_index={s["id"]: s for s in catalog if "id" in s},
            price_per_song=body.get("price_per_song", 0.0),
            price_for_three=body.get("price_for_three", 0.0),
            currency=body.get("currency", ""),
            stripe_enabled=body.get("stripe_enabled", bool(pk)),
            stripe_publishable_key=pk,
            stripe_secret_key=body.get("stripe_secret_key", ""),
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
    if "is_playing" in body:
        bar.is_playing = bool(body["is_playing"])
    return {"ok": True}


@app.post("/api/host/unregister")
async def host_unregister(body: dict[str, Any]):
    """
    Called by iOS/Android when End Session is triggered.
    Deletes the BarSession immediately so stale admin/bartender URLs and QR codes get 404.
    Idempotent — returns 200 even if the bar is already gone.
    """
    jukebar_id = body.get("jukebar_id", "")
    session    = body.get("session", "")
    bar = _bars.get(jukebar_id)
    if bar is None:
        return {"ok": True}
    if bar.session != session:
        raise HTTPException(403, "Invalid session")
    del _bars[jukebar_id]
    return {"ok": True}


@app.post("/api/host/sync")
async def host_sync(body: dict[str, Any]):
    """
    Called by host every 5 s.

    Host sends:
      played_request_ids   — request IDs whose song just became currentSong
      approved_request_ids — request IDs approved by the Android bartender screen
                             (server sets these to "approved" so kiosk Up Next shows them)
      settings              — {field: bool}; the host's current values for
                             bartender_enabled/stripe_enabled/accepting_requests, sent
                             unconditionally on every call. Trusted outright and applied
                             immediately — the host is the only thing that actually knows
                             its own settings, and it re-sends them every 5s regardless, so
                             a single dropped or out-of-order call self-heals on the next tick.

    Server returns:
      requests         — new customer requests since last sync (status == "pending")
      actions          — queued bartender approve/deny/stop_session actions, then clears the queue
      desired_settings — {field: bool} for any field an admin/bartender has requested a
                         change for that this host's echo hasn't matched yet. Host should
                         apply these locally; once a later echo matches, the entry drops out
                         of future responses on its own — no separate ack needed.
    """
    jukebar_id = body.get("jukebar_id", "")
    session    = body.get("session", "")
    bar = _get_bar(jukebar_id)
    _validate_session(bar, session)
    _touch(bar)

    # Host's settings echo — always trusted as current truth (see docstring above).
    # Separately, clear a desired_settings entry once the echo matches it. Compare against
    # whatever's CURRENTLY desired, not a stale snapshot of what was true when some earlier
    # request went out — if a newer request changed the desired value in the meantime, this
    # simply won't match and the entry stays pending, re-sent as-is on the next response.
    echoed = body.get("settings", {})
    for field_name in ("bartender_enabled", "stripe_enabled", "accepting_requests"):
        if field_name not in echoed:
            continue
        value = bool(echoed[field_name])
        setattr(bar, field_name, value)
        if bar.desired_settings.get(field_name) == value:
            bar.desired_settings.pop(field_name, None)

    # Android bartender screen: IDs approved natively on the device
    for rid in body.get("approved_request_ids", []):
        if rid in bar.requests and bar.requests[rid].status == "pending":
            bar.requests[rid].status = "approved"
            bar.requests[rid].approved_at = time.time()

    for rid in body.get("played_request_ids", []):
        if rid in bar.requests:
            bar.requests[rid].status = "played"

    if "up_next" in body:
        bar.up_next_queue = body["up_next"]
        # Host's up_next is the authority: mark any request the host has queued as approved
        up_next_ids = {item.get("request_id") for item in body["up_next"] if item.get("request_id")}
        for rid in up_next_ids:
            if rid in bar.requests and bar.requests[rid].status == "pending":
                bar.requests[rid].status = "approved"
                bar.requests[rid].approved_at = time.time()

    new_requests = [
        {
            "id": r.id,
            "song_ids": r.song_ids,
            "song_titles": r.song_titles,
            "requester_name": r.requester_name,
            "jump": r.jump,
            "paid": r.paid,
        }
        for r in bar.requests.values()
        if r.status == "pending"
    ]

    actions = list(bar.pending_actions)
    bar.pending_actions.clear()

    return {"requests": new_requests, "actions": actions, "desired_settings": dict(bar.desired_settings)}


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
            "bar_name":               bar.bar_name,
            "catalog":                bar.catalog,
            "require_approval":       bar.require_approval,
            "bartender_enabled":      bar.bartender_enabled,
            "stripe_enabled":         bar.stripe_enabled,
            "price_per_song":         bar.price_per_song,
            "price_for_three":        bar.price_for_three,
            "currency":               bar.currency,
            "stripe_publishable_key": bar.stripe_publishable_key if bar.stripe_enabled else "",
            "accepting_requests":     bar.accepting_requests,
            "settings_pending":       list(bar.desired_settings.keys()),
        },
        headers={"Cache-Control": "no-store"},
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


@app.get("/api/bar/{jukebar_id}/genres")
async def bar_genres(jukebar_id: str, playlist: str | None = Query(default=None)):
    """Return the profiling section for one playlist.

    playlist param is optional — if omitted, falls back to the bar's currently
    registered playlist_name (so the customer page can call this without knowing it).
    """
    raw_profile = _bar_profiles.get(jukebar_id)
    if not raw_profile:
        raise HTTPException(404, "No profile for this bar yet")
    bar = _bars.get(jukebar_id)
    resolved = playlist or (bar.playlist_name if bar else "")
    if not resolved:
        raise HTTPException(404, "Playlist not specified and bar has no registered playlist")
    pl_data = raw_profile.get("playlists", {}).get(resolved)
    if not pl_data:
        raise HTTPException(404, f"Playlist '{resolved}' not found in profile")
    return pl_data


@app.get("/api/bar/{jukebar_id}/nowplaying")
async def bar_nowplaying(jukebar_id: str, s: str = Query(..., alias="s")):
    bar = _customer_bar(jukebar_id, s)
    return {"now_playing": bar.now_playing, "is_playing": bar.is_playing}


@app.get("/api/bar/{jukebar_id}/display")
async def bar_display(jukebar_id: str, s: str = Query(None)):
    """Session-agnostic kiosk endpoint: now-playing + approved requests. No session required."""
    bar = _get_bar(jukebar_id)
    _touch(bar)
    session_valid = s is None or bar.session == s
    up_next = [
        {
            "id":             item.get("request_id", ""),
            "song_ids":       item.get("song_ids", []),
            "song_titles":    [bar.song_index.get(sid, {}).get("title", "") for sid in item.get("song_ids", [])],
            "song_details":   [
                {
                    "artist":           bar.song_index.get(sid, {}).get("artist", ""),
                    "album":            bar.song_index.get(sid, {}).get("album", ""),
                    "title":            bar.song_index.get(sid, {}).get("title", ""),
                    "duration_seconds": bar.song_index.get(sid, {}).get("duration_seconds", 0),
                }
                for sid in item.get("song_ids", [])
            ],
            "requester_name": item.get("requester_name", ""),
            "jump":           item.get("jump", False),
            "status":         "approved_jump" if item.get("jump") else "approved",
            "approved_at":    item.get("approved_at", 0),
            "created_at":     item.get("approved_at", 0),
            "paid":           item.get("paid", False),
        }
        for item in bar.up_next_queue
    ]
    return {
        "now_playing":            bar.now_playing,
        "requests":               up_next,
        "session_valid":          session_valid,
        "require_approval":       bar.require_approval,
        "bartender_enabled":      bar.bartender_enabled,
        "stripe_enabled":         bar.stripe_enabled,
        "stripe_publishable_key": bar.stripe_publishable_key if bar.stripe_enabled else "",
        "accepting_requests":     bar.accepting_requests,
    }


@app.post("/api/bar/{jukebar_id}/request")
async def bar_request(jukebar_id: str, s: str = Query(..., alias="s"), body: dict[str, Any] = ...):
    bar = _customer_bar(jukebar_id, s)
    if not bar.accepting_requests:
        raise HTTPException(403, "Not accepting requests right now")

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
        status="pending",  # always pending; host confirms via up_next — relay is not the authority
    )
    bar.requests[rid] = req
    return {"request_id": rid, "status": req.status}


@app.get("/api/bar/{jukebar_id}/request/{request_id}")
async def bar_request_status(jukebar_id: str, request_id: str, s: str = Query(..., alias="s")):
    bar = _customer_bar(jukebar_id, s)
    req = bar.requests.get(request_id)
    if req is None:
        raise HTTPException(404, "Request not found")
    return {"status": req.status, "song_titles": req.song_titles}


# ---------------------------------------------------------------------------
# Stripe payment endpoints (internet mode)
# ---------------------------------------------------------------------------

_STRIPE_ZERO_DECIMAL = {
    "bif","clp","djf","gnf","jpy","kmf","krw","mga","pyg","rwf","ugx","vnd","vuv","xaf","xof","xpf"
}

def _to_stripe_amount(price: float, currency: str) -> int:
    if currency.lower() in _STRIPE_ZERO_DECIMAL:
        return int(price)
    return int(round(price * 100))


@app.post("/api/bar/{jukebar_id}/create-payment-intent")
async def bar_create_payment_intent(
    jukebar_id: str, s: str = Query(..., alias="s"), body: dict[str, Any] = ...
):
    import httpx
    bar = _customer_bar(jukebar_id, s)
    if not bar.accepting_requests:
        raise HTTPException(403, "Not accepting requests right now")
    if not bar.stripe_secret_key:
        raise HTTPException(400, "Stripe not configured")
    song_ids: list[str] = body.get("song_ids", [])
    if not song_ids:
        raise HTTPException(400, "song_ids required")
    pps = bar.price_per_song
    p3  = bar.price_for_three
    if pps <= 0:
        raise HTTPException(400, "No price configured")
    price    = p3 if (len(song_ids) == 3 and p3 > 0) else pps * len(song_ids)
    currency = bar.currency.lower()
    if not currency:
        raise HTTPException(400, "Stripe currency not set")
    amount = _to_stripe_amount(price, currency)

    song_titles   = body.get("song_titles", song_ids)
    requester_name = body.get("requester_name", "") or "Anonymous"
    song_list     = ", ".join(song_titles)
    date_str      = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    description   = f"JukeBar {bar.bar_name} jukebox request for {song_list} made under name {requester_name} at {date_str}"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.stripe.com/v1/payment_intents",
            data={"amount": amount, "currency": currency, "automatic_payment_methods[enabled]": "true",
                  "description": description},
            auth=(bar.stripe_secret_key, ""),
            timeout=15.0,
        )
    if resp.status_code not in range(200, 300):
        try:
            msg = resp.json()["error"]["message"]
        except Exception:
            msg = f"Stripe error {resp.status_code}"
        raise HTTPException(502, msg)
    pi = resp.json()
    return {"client_secret": pi["client_secret"], "payment_intent_id": pi["id"], "currency": currency, "amount": amount}


@app.post("/api/bar/{jukebar_id}/payment-confirmed")
async def bar_payment_confirmed(
    jukebar_id: str, s: str = Query(..., alias="s"), body: dict[str, Any] = ...
):
    import httpx
    bar = _customer_bar(jukebar_id, s)
    if not bar.stripe_secret_key:
        raise HTTPException(400, "Stripe not configured")
    pi_id = body.get("payment_intent_id", "")
    if not pi_id:
        raise HTTPException(400, "payment_intent_id required")
    song_ids: list[str] = body.get("song_ids", [])
    if not song_ids:
        raise HTTPException(400, "song_ids required")

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.stripe.com/v1/payment_intents/{pi_id}",
            auth=(bar.stripe_secret_key, ""),
            timeout=15.0,
        )
    if resp.status_code not in range(200, 300):
        raise HTTPException(502, f"Stripe verify error {resp.status_code}")
    pi_status = resp.json().get("status", "")
    if pi_status != "succeeded":
        raise HTTPException(402, f"Payment not completed (status={pi_status})")

    rid           = str(uuid.uuid4())
    requester_name = body.get("requester_name", "Anonymous")
    customer_id   = body.get("customer_id", "")
    song_titles   = [bar.song_index.get(sid, {}).get("title", "") for sid in song_ids]
    req = SongRequest(
        id=rid,
        song_ids=song_ids,
        song_titles=song_titles,
        requester_name=requester_name,
        customer_id=customer_id,
        jump=False,
        status="pending",   # host picks this up via new_requests (paid=True → auto-inject)
        paid=True,
        payment_method="stripe",
    )
    bar.requests[rid] = req
    return {"request_id": rid, "status": "approved"}


# ---------------------------------------------------------------------------
# Bartender endpoints (internet mode)
# ---------------------------------------------------------------------------

@app.get("/api/bar/{jukebar_id}/requests")
async def bartender_requests(jukebar_id: str, s: str = Query(..., alias="s")):
    """
    Feeds admin.html/bartender.html's Requests (needs a bartender tap) and Up Next (already
    resolved) sections. "status" here is a DISPLAY status, not always bar.requests[rid].status
    verbatim: a still-"pending" request is shown as "approved" (Up Next, no buttons) the moment
    it's destined to be auto-approved with no human review at all - Stripe-paid ones always
    (payment_method == "stripe"), and any request at all when the bar's current mode needs no
    approval (require_approval is False, i.e. both stripe_enabled and bartender_enabled are off).
    The underlying bar.requests[rid].status is left untouched by this - it stays "pending" until
    the host's own up_next echo confirms it (see host_sync()), since that's also what makes it
    show up in new_requests for the host to actually pick up and inject at all. This is purely a
    display override so the bartender never sees a fleeting, meaningless "needs approval" flash
    for something nobody was ever going to be asked to approve.
    """
    bar = _customer_bar(jukebar_id, s)
    pending = [
        {
            "id": r.id,
            "song_ids": r.song_ids,
            "song_titles": r.song_titles,
            "song_details": [
                {
                    "artist":           bar.song_index.get(sid, {}).get("artist", ""),
                    "album":            bar.song_index.get(sid, {}).get("album", ""),
                    "title":            bar.song_index.get(sid, {}).get("title", ""),
                    "duration_seconds": bar.song_index.get(sid, {}).get("duration_seconds", 0),
                }
                for sid in r.song_ids
            ],
            "requester_name": r.requester_name,
            "customer_id": r.customer_id,
            "jump": r.jump,
            "status": (
                "approved"
                if r.status == "pending" and (r.payment_method == "stripe" or not bar.require_approval)
                else r.status
            ),
            "paid": r.paid,
            "payment_method": r.payment_method,
            "created_at": r.created_at,
            "approved_at": r.approved_at,
        }
        for r in sorted(bar.requests.values(), key=lambda r: r.created_at)
        if r.status in ("pending", "approved", "approved_jump")
    ]
    return {"bar_name": bar.bar_name, "requests": pending, "now_playing": bar.now_playing,
            "price_per_song": bar.price_per_song, "price_for_three": bar.price_for_three,
            "currency": bar.currency, "require_approval": bar.require_approval,
            "stripe_enabled": bar.stripe_enabled, "bartender_enabled": bar.bartender_enabled,
            "accepting_requests": bar.accepting_requests,
            "settings_pending": list(bar.desired_settings.keys())}


@app.post("/api/bar/{jukebar_id}/approve")
async def bartender_approve(jukebar_id: str, s: str = Query(..., alias="s"), body: dict[str, Any] = ...):
    bar = _customer_bar(jukebar_id, s)
    rid = body.get("request_id", "")
    req = bar.requests.get(rid)
    if req is None:
        raise HTTPException(404, "Request not found")
    jump = body.get("jump", req.jump)
    req.jump = jump
    # A bartender manually approving a pending request IS the payment confirmation (cash/card
    # collected in person) - mark it paid unless it somehow already came through Stripe.
    if req.payment_method != "stripe":
        req.paid = True
        req.payment_method = "bartender"
    # Don't mark approved here — host confirms via up_next on next sync
    bar.pending_actions.append({
        "type": "approve",
        "request_id": rid,
        "song_ids": req.song_ids,
        "jump": jump,
    })
    return {"ok": True}


@app.get("/api/bar/{jukebar_id}/history")
async def bar_history(jukebar_id: str, s: str = Query(..., alias="s")):
    """
    All requests for the current session, any status - used by admin page's Reports tab (status
    counts) and the Past Requests overlay (played/denied list). Shaped the same as
    bartender_requests()'s entries (song_details, payment_method, etc.) so the client can reuse
    the same requestCard() renderer for both.
    """
    bar = _customer_bar(jukebar_id, s)
    return {
        "bar_name": bar.bar_name,
        "requests": [
            {
                "id": r.id,
                "song_ids": r.song_ids,
                "song_titles": r.song_titles,
                "song_details": [
                    {
                        "artist":           bar.song_index.get(sid, {}).get("artist", ""),
                        "album":            bar.song_index.get(sid, {}).get("album", ""),
                        "title":            bar.song_index.get(sid, {}).get("title", ""),
                        "duration_seconds": bar.song_index.get(sid, {}).get("duration_seconds", 0),
                    }
                    for sid in r.song_ids
                ],
                "requester_name": r.requester_name,
                "customer_id": r.customer_id,
                "status": r.status,
                "jump": r.jump,
                "paid": r.paid,
                "payment_method": r.payment_method,
                "created_at": r.created_at,
                "approved_at": r.approved_at,
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
    # Optimistically update is_playing so fetchNowPlaying reflects the new state
    # immediately — before Android picks up the action on its next sync cycle.
    if action == "pause":
        bar.is_playing = False
    elif action == "play":
        bar.is_playing = True
    return {"ok": True}


@app.post("/api/bar/{jukebar_id}/deny")
async def bartender_deny(jukebar_id: str, s: str = Query(..., alias="s"), body: dict[str, Any] = ...):
    """
    Denies a pending request, or cancels one already approved/queued (the "Cancel" button on
    already-playing-soon free requests). Stripe-paid requests can never be denied/cancelled
    through this endpoint, at any status - payment_method is set to "stripe" at creation and
    never changes, so this one check covers both "still pending, not yet host-confirmed" and
    "already approved" for that case. A bartender-paid request only becomes payment_method
    "bartender" at the moment it's actually approved (see bartender_approve()), so a still-
    pending bartender-flow request (payment_method still "free" at that point) can still be
    denied before approval - that's the pre-existing reject-before-it-plays behavior, unrelated
    to this restriction.
    """
    bar = _customer_bar(jukebar_id, s)
    rid = body.get("request_id", "")
    req = bar.requests.get(rid)
    if req is None:
        raise HTTPException(404, "Request not found")
    if req.payment_method != "free":
        raise HTTPException(403, "Paid requests cannot be denied or cancelled")
    req.status = "denied"
    bar.pending_actions.append({
        "type": "deny",
        "request_id": rid,
    })
    return {"ok": True}


@app.post("/api/bar/{jukebar_id}/stop")
async def bar_stop(jukebar_id: str, s: str = Query(..., alias="s")):
    """Queue a stop_session action — iOS picks it up on the next sync and rotates the session."""
    bar = _customer_bar(jukebar_id, s)
    bar.pending_actions.append({"type": "stop_session"})
    return {"ok": True}


@app.post("/api/bar/{jukebar_id}/settings")
async def bar_settings(jukebar_id: str, s: str = Query(..., alias="s"), body: dict[str, Any] = ...):
    """
    Admin/bartender: record the desired value for one or more settings fields. Does NOT touch
    bar.bartender_enabled/stripe_enabled/accepting_requests directly - the host picks up
    desired_settings on its next /api/host/sync call, applies it locally, and its own echoed
    current value is what actually updates the field (see host_sync()). A newer request for
    the same field before the host catches up just overwrites the desired value in place -
    only the latest matters, nothing is queued or ordered.
    """
    bar = _customer_bar(jukebar_id, s)
    for field_name in ("bartender_enabled", "stripe_enabled", "accepting_requests"):
        if field_name in body:
            bar.desired_settings[field_name] = bool(body[field_name])
    return {"ok": True}
