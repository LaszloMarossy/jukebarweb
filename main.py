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
import io
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import qrcode
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
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
    # When this request actually resolved (played or denied) - not creation or approval time.
    # Set directly by bartender_deny() for relay-driven denials, and via the host's requests
    # echo for played ones (which only the host can detect). Powers "most recently resolved
    # first" ordering on admin.html's Reports tab.
    resolved_at: float = 0.0
    paid: bool = False
    # "free" (auto-accepted, no payment) | "bartender" (cash/card collected in person, bartender
    # approval IS the payment confirmation) | "stripe" (paid online). paid is true for both
    # bartender and stripe - only "free" means no payment happened at all.
    payment_method: str = "free"
    # Frozen at request creation (mirrors iOS/Android's immutable SongRequest.price) so historical
    # rows still show what was actually charged/quoted even if the bar's pricing changes later.
    price: float = 0.0
    # Source IP at creation time (2026-08-28) — used only for the per-requester outstanding-
    # request throttle in bar_request(), same idiom as bar_authenticate()'s client_ip. Not sent
    # to or displayed on any host/admin surface; purely a relay-local anti-flood signal.
    requester_ip: str = ""


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
    # True while Android's PlaybackCoordinator is blocked waiting for someone to physically
    # reconnect Spotify at the kiosk (2026-08-08) — distinct from is_playing==False so
    # admin.html can tell "someone tapped pause" apart from "the system had to stop because
    # Spotify disconnected" and disable/warn on the Play button instead of letting a remote
    # admin resume into an immediate re-failure. iOS has no equivalent, always False for it.
    spotify_outage_active: bool = False
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    requests: dict[str, SongRequest] = field(default_factory=dict)
    pending_actions: list[dict] = field(default_factory=list)
    up_next_queue: list[dict] = field(default_factory=list)  # authoritative host queue, pushed every sync
    pin_hash: str = ""  # SHA-256 hex of admin PIN — set at register; required for admin.html auth
    # SHA-256 hex of bartender PIN — separate secret from pin_hash (2026-08). Optional: empty
    # means the bartender role is entirely off for this bar (no bartender QR, no bartender.html
    # reachable, no pairing) — admin handles approve/deny/control/settings directly instead.
    bartender_pin_hash: str = ""
    # token -> {"name": str, "paired_at": float, "ip": str}. Minted by bar_authenticate() on a
    # correct PIN; required by every bartender/admin action endpoint below (approve/deny/control/
    # settings/requests/history) via _require_bartender_token() — the PIN check alone used to be
    # purely a client-side UI gate with zero server enforcement, since those endpoints previously
    # only validated the same session token the public customer QR code also carries. Fixed 2026-08.
    bartender_tokens: dict[str, dict] = field(default_factory=dict)
    # field -> value an admin/bartender asked for that the host hasn't echoed back yet;
    # presence = pending. Latest write per field wins, no queue — see bar_settings()/host_sync().
    # bool for the three toggle fields, str for bartender_pin_hash (empty string is a valid
    # desired value — it means "admin wants to turn bartender access off"), bool/int for the
    # auto-manage-requests fields below.
    desired_settings: dict[str, bool | str | int] = field(default_factory=dict)
    # Request-management mode (2026-08-28): False (default) = manual, admin controls
    # accepting_requests directly via the existing Start/Stop button. True = auto — the HOST
    # (never the relay) watches its own live outstanding-request count every tick and flips
    # accepting_requests on/off itself, using the two watermarks below, then broadcasts the
    # result out through the exact same accepting_requests echo path manual mode already uses —
    # no new wire concept, this field only tells the host which logic gets to drive that one
    # existing field. Mirrors the raw-stored-preference pattern of the other toggles: this and
    # the two thresholds below always echo the host's actual configured values, never something
    # the relay computes or overrides.
    auto_manage_requests: bool = False
    # Outstanding-request count at or above which the host stops accepting new requests. Default
    # 10. "Outstanding" = approved/up-next only, NOT pending-awaiting-bartender-review (2026-08-28
    # refinement, decided after this shipped) — a still-pending pay-to-bartender request hasn't
    # been reviewed or paid for yet (a bartender's Approve tap IS the payment confirmation, see
    # payment_method notes above), so counting it would let someone flood-submit no-show requests
    # specifically to trip this watermark before a human ever looks at them. Computed host-side
    # from the host's own local request store, never by the relay - this comment just documents
    # the semantics the host is expected to apply, not something enforced here.
    auto_manage_max: int = 10
    # Outstanding-request count at or below which the host resumes accepting. Default 5. Must
    # stay <= auto_manage_max for the hysteresis band to make sense; the host is responsible for
    # applying that ordering, the relay just stores and forwards whatever it's told. Both fields
    # at 0 means the feature is inert even if auto_manage_requests is true (e.g. before an
    # operator has filled in real numbers yet) — the host skips evaluation entirely in that case.
    auto_manage_restart: int = 5
    # "localOnly" | "localAndRemote" | "remoteOnly" (2026-07-22+) — governs CUSTOMERS only, set at
    # register time from the host's own wizard choice. "localOnly" is a hard lockout enforced here
    # too (bar_request()/bar_create_payment_intent()/bar_payment_confirmed()/bar_page()), not just
    # on the host's LAN server — a bar using Internet transport with kioskMode=localOnly must not
    # have its relay-served customer page/endpoints reachable either.
    kiosk_mode: str = "localAndRemote"
    # filename -> {"content": str (raw CSV text), "created_at": float}. Mirrors the host's local
    # report files (2026-08-07) — the relay imposes no cap of its own (the host's own local
    # retention, capped at 20, is the only real limit); this dict is reconciled to exactly match
    # whatever the host currently has on every register/sync (see host_register()/host_sync()),
    # same self-healing full-reassert pattern as bar.requests/settings — a relay restart just
    # refills from the host's next sync, no special recovery logic needed. Content only arrives via
    # the dedicated host_report_upload() endpoint (event-driven, called once when the host actually
    # generates a new report) — kept out of the regular 5s sync payload since CSVs can be
    # nontrivially sized and most ticks have nothing new to send.
    reports: dict[str, dict] = field(default_factory=dict)

    @property
    def require_approval(self) -> bool:
        # stripe_enabled can still read True while kiosk_mode is "localOnly" - the host reports
        # its raw stored preference (so admin.html/bartender.html can show the toggle checked but
        # greyed out, preserving what the operator will get back once they switch modes), but
        # Stripe is functionally inert in that mode (no customer page exists to ever use it from).
        # Treating it as still "requiring approval" here would make a bar with Stripe on but
        # Bartender Pay just turned off look like it still needs approval, when it's actually
        # free/auto-accept now - hiding the kiosk's own Request button for no real reason.
        effective_stripe = self.stripe_enabled and self.kiosk_mode != "localOnly"
        return effective_stripe or self.bartender_enabled

    @property
    def effective_accepting_requests(self) -> bool:
        # Paused playback implicitly blocks new request submission (2026-08-02) — customers can
        # still browse the catalog, they just can't submit while nothing's playing. Replaces the
        # old 30-min-idle-pause timer (host-local, iOS/Android only, relay was never involved in
        # it), which existed to stop an overnight session from still accepting requests if the
        # bartender went home without ending it. Since paused now always blocks new requests
        # regardless of how long, that whole timer/session-rotation mechanism became unnecessary
        # and was removed on both host platforms the same day — an accidental Pause tap no longer
        # has any escalating consequence, and "did you forget to End Session" is now entirely the
        # operator's own responsibility (their words: "it is on them").
        #
        # Same raw-vs-effective split as require_approval above: bar.accepting_requests itself is
        # never touched by this — admin.html/bartender.html's toggle keeps showing the operator's
        # actual configured preference, not silently flipped off. Only the two customer-facing
        # surfaces that actually gate a submission (bar_request(), bar_create_payment_intent(), and
        # bar_catalog()'s echo to customer.html so the Request button doesn't even show as usable)
        # read this property; bar_payment_confirmed() deliberately does not — a payment intent that
        # already succeeded must still be honored even if playback paused in the meantime.
        return self.accepting_requests and self.is_playing


_map_entries: dict[str, MapEntry] = {}   # persisted to disk
_bars: dict[str, BarSession] = {}        # in-memory only
_bar_profiles: dict[str, dict] = {}     # bar_id → profile.json contents; refreshed from GCS every PROFILE_CACHE_TTL s
_profiles_loaded_at: float = 0.0

# Per-(bar, source IP, role) PIN attempt tracking for /api/bar/{id}/authenticate — deliberately
# NOT global/per-bar-only, so one IP fumbling the PIN doesn't lock out a different bartender pairing
# from their own device, and keyed separately by role (2026-08 PIN split) so repeated bad bartender
# guesses from a shared bar IP don't also lock out the admin PIN from that same IP, or vice versa.
# In-memory only (matches BarSession) - a relay restart clears all lockouts, same accepted
# tradeoff as bar.requests.
_bartender_lockouts: dict[tuple[str, str, str], dict[str, float]] = {}
BARTENDER_LOCKOUT_MAX_ATTEMPTS = 3
BARTENDER_LOCKOUT_SECONDS = 15 * 60

# Per-requester outstanding-request throttle (2026-08-28) — an anonymous, unauthenticated guest
# (free or pay-to-bartender path only; Stripe is self-limiting since it costs real money) could
# otherwise flood-submit requests purely to clutter the admin/bartender Requests screen. Capped
# at MAX_OUTSTANDING_REQUESTS_PER_REQUESTER (not yet played/denied) per requester - identity is
# the UNION of two independent signals, not their intersection, deliberately: source IP (reliable
# on LAN, where each device gets its own DHCP-assigned address, same assumption the bartender-PIN
# lockout above already relies on) and customer_id (the browser-persisted localStorage id
# customer.html already sends with every request, survives a network change but not a cleared
# browser). Matching on EITHER means an abuser has to defeat BOTH signals at once (get a new IP
# *and* a fresh customer_id) to reset their count - matching just one doesn't help them evade.
# Same "raise the bar for casual abuse, not hacker-proof" philosophy as the PIN lockout's own
# accepted per-IP limitation. Only checked in bar_request() (the free/pay-to-bartender creation
# endpoint) - not create-payment-intent/payment-confirmed, since a completed Stripe charge is
# already a real cost to the requester and a request row there is proof of actual payment, not
# spam.
MAX_OUTSTANDING_REQUESTS_PER_REQUESTER = 2


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

def _reconcile_reports(bar: BarSession, filenames: list[str] | None) -> list[str]:
    """Drop any cached report the host no longer has locally, and return the filenames the host
    says exist that the relay has no content for yet — same full-reassert-not-delta pattern as
    bar.requests/settings, extended with a backfill signal since report content (unlike small
    settings fields) isn't cheap enough to resend in full every tick.

    `filenames` is the host's complete current list, sent on every register/sync call:
    - Anything cached here but absent from it was deleted on the host (locally by an admin, or by
      a delete_report pending_action already applied) and should vanish from the relay's mirror too.
    - Anything in it the relay has no content for (a fresh register after a genuine new session
      replaced the BarSession outright, a relay restart, or a previous upload that never landed)
      gets returned here so the caller can ask the host to re-upload it — content only ever
      arrives via the dedicated upload endpoint, so reconciliation alone can prune but never
      restore it; without this the relay's mirror would silently go stale after any of those resets.

    Missing/None `filenames` (older host build) leaves the cache untouched and requests nothing,
    rather than wiping everything on a stale-client false assumption.
    """
    if filenames is None:
        return []
    keep = set(filenames)
    for stale in [f for f in bar.reports if f not in keep]:
        del bar.reports[stale]
    return [f for f in filenames if f not in bar.reports]


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

    Optional `report_filenames` (list[str]): the host's complete current local report list -
    reconciled against bar.reports the same self-healing way as everything else here (see
    _reconcile_reports()). Returns `reports_needed` (list[str]): filenames the host should
    (re-)upload via POST /api/host/report_upload - always the full list on a genuine new session
    (the fresh BarSession has no cached content yet), otherwise just whatever isn't already cached.
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
        bar.bartender_pin_hash = body.get("bartender_pin_hash", "")
        bar.kiosk_mode = body.get("kiosk_mode", "localAndRemote")
        bar.auto_manage_requests = body.get("auto_manage_requests", False)
        bar.auto_manage_max = int(body.get("auto_manage_max", 10))
        bar.auto_manage_restart = int(body.get("auto_manage_restart", 5))
        reports_needed = _reconcile_reports(bar, body.get("report_filenames"))
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
            bartender_pin_hash=body.get("bartender_pin_hash", ""),
            kiosk_mode=body.get("kiosk_mode", "localAndRemote"),
            auto_manage_requests=body.get("auto_manage_requests", False),
            auto_manage_max=int(body.get("auto_manage_max", 10)),
            auto_manage_restart=int(body.get("auto_manage_restart", 5)),
        )
        # Fresh BarSession's reports dict starts empty regardless of what the host already has
        # locally (a genuine new session replaces the object outright) - everything the host
        # lists needs re-uploading, same as any other reconcile-found-nothing-cached case.
        reports_needed = _reconcile_reports(_bars[jukebar_id], body.get("report_filenames"))

    # Keep map entry's last_seen fresh if the bar is registered there
    if jukebar_id in _map_entries:
        _map_entries[jukebar_id].last_seen = time.time()

    return {"ok": True, "reports_needed": reports_needed}


@app.post("/api/host/nowplaying")
async def host_nowplaying(body: dict[str, Any]):
    """
    Called by iOS/Android immediately when the now-playing item (or play state) changes.
    Lightweight alternative to waiting for the next /sync cycle — despite the name, this is
    Android's actual per-change push path for is_playing/spotify_outage_active too, not host_sync().
    """
    jukebar_id = body.get("jukebar_id", "")
    session    = body.get("session", "")
    bar = _get_bar(jukebar_id)
    _validate_session(bar, session)
    _touch(bar)
    bar.now_playing = body.get("now_playing")  # full song dict or null
    if "is_playing" in body:
        bar.is_playing = bool(body["is_playing"])
    if "spotify_outage_active" in body:
        bar.spotify_outage_active = bool(body["spotify_outage_active"])
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
      requests              — host's full local request list, any status (id, song_ids,
                             requester_name, customer_id, status, jump, paid, payment_method,
                             price, created_at, approved_at). Sent unconditionally every call,
                             same self-healing pattern as settings below — upserted into
                             bar.requests (see merge loop). This is what makes a request born
                             on ANY host-local surface (kiosk, LAN web, internet-adopted) visible
                             relay-side without a client-specific POST: the host is sole
                             authority for its own state, and this broadcasts it out every tick.
                             Both iOS (LocalStorage) and Android (LocalRequestManager) send this
                             as their single, fully unified request store as of 2026-07-20 — see
                             CLAUDE.md's host-broadcasts-state note.
      up_next                — host's live queue, for bar_display() only (not request status —
                             see requests above).
      settings              — {field: bool | str | int}; the host's current values for
                             bartender_enabled/stripe_enabled/accepting_requests/
                             auto_manage_requests (bool), bartender_pin_hash (str, "" meaning
                             bartender role off), and auto_manage_max/auto_manage_restart (int),
                             sent unconditionally on every call. Trusted outright and applied
                             immediately — the host is the only thing that actually knows
                             its own settings, and it re-sends them every 5s regardless, so
                             a single dropped or out-of-order call self-heals on the next tick.
                             auto_manage_requests being true means the HOST is the one deciding
                             accepting_requests's value every tick (see BarSession docstring) —
                             the relay still just echoes whatever accepting_requests value shows
                             up here, same as always, with no special-casing for which mode
                             produced it.

    Server returns:
      requests         — new customer requests since last sync (status == "pending"), including
                         price/payment_method so the host doesn't have to recompute/guess them
      actions          — queued bartender approve/deny/control actions, then clears the queue
      desired_settings — {field: bool | str} for any field an admin/bartender has requested a
                         change for that this host's echo hasn't matched yet. Host should
                         apply these locally; once a later echo matches, the entry drops out
                         of future responses on its own — no separate ack needed.
      reports_needed    — filenames (from the optional report_filenames the host sent) the relay
                         has no content cached for and the host should (re-)upload via
                         POST /api/host/report_upload. See _reconcile_reports().

    Host may also send `report_filenames` (list[str], optional) — same reconciliation as
    host_register()'s. `actions` can now include {"type": "generate_report"} (queued by
    POST /api/bar/{id}/reports/generate) and {"type": "delete_report", "filename": ...} (queued
    when an admin successfully downloads a report via GET /api/bar/{id}/reports/{filename}).
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
    for field_name in ("bartender_enabled", "stripe_enabled", "accepting_requests", "auto_manage_requests"):
        if field_name not in echoed:
            continue
        value = bool(echoed[field_name])
        setattr(bar, field_name, value)
        if bar.desired_settings.get(field_name) == value:
            bar.desired_settings.pop(field_name, None)

    # auto_manage_max/auto_manage_restart: same self-healing echo pattern as the bools above, but
    # int-valued rather than bool-valued.
    for field_name in ("auto_manage_max", "auto_manage_restart"):
        if field_name not in echoed:
            continue
        value = int(echoed[field_name])
        setattr(bar, field_name, value)
        if bar.desired_settings.get(field_name) == value:
            bar.desired_settings.pop(field_name, None)

    # bartender_pin_hash: same self-healing echo pattern as the bool settings above, but a
    # string — empty is a legitimate value (bartender role off), not "field absent".
    if "bartender_pin_hash" in echoed:
        pin_value = str(echoed["bartender_pin_hash"] or "")
        bar.bartender_pin_hash = pin_value
        if bar.desired_settings.get("bartender_pin_hash") == pin_value:
            bar.desired_settings.pop("bartender_pin_hash", None)
        # Turning bartender access off must also kill any already-paired bartender sessions
        # (2026-08-18) — previously bartender_tokens was untouched here, so an existing token
        # kept working indefinitely (bartender_requests()/etc. only check token presence, never
        # bar.bartender_pin_hash) and the Sessions tab kept listing it as "active" forever, since
        # nothing ever removed it. Mirrors bartender_sessions_kill()'s direct deletion — no host
        # round-trip needed for this either, this is relay-side session bookkeeping, not
        # something the host owns. Admin tokens (role == "admin") are untouched; only bartender
        # role tokens are purged.
        if not pin_value:
            bar.bartender_tokens = {
                tok: rec for tok, rec in bar.bartender_tokens.items()
                if rec.get("role") != "bartender"
            }

    if "up_next" in body:
        bar.up_next_queue = body["up_next"]

    # Host's full request-list broadcast (see docstring): upsert only, never delete — a
    # customer-web/Stripe request the relay just created may not be in this echo yet (host
    # hasn't adopted it via new_requests below on this tick), and old history the host has
    # stopped keeping locally shouldn't vanish from the relay's view just because it aged out
    # of the echo. For an id the relay already knows about (born here via bar_request()/
    # bar_payment_confirmed()), only status/jump/approved_at/paid are trusted from the host —
    # price/payment_method/requester_name/song_ids stay whatever the relay originally recorded,
    # since that's the authoritative source for anything born on the relay, not a value the host
    # independently reconstructed. For an id the relay has never seen (born host-side — kiosk or
    # LAN web), the host's echo is the only source of truth for every field.
    for r in body.get("requests", []):
        rid = r.get("id")
        if not rid:
            continue
        existing = bar.requests.get(rid)
        if existing is not None:
            existing.status = r.get("status", existing.status)
            existing.jump = bool(r.get("jump", existing.jump))
            if r.get("approved_at"):
                existing.approved_at = r["approved_at"]
            # Only the host can detect "played" (relay already stamps its own denials directly
            # in bartender_deny()) - trust its resolved_at whenever it sends one.
            if r.get("resolved_at"):
                existing.resolved_at = r["resolved_at"]
            if r.get("paid"):
                existing.paid = True
        else:
            bar.requests[rid] = SongRequest(
                id=rid,
                song_ids=r.get("song_ids", []),
                song_titles=r.get("song_titles", []),
                requester_name=r.get("requester_name", ""),
                customer_id=r.get("customer_id", "HOST"),
                jump=bool(r.get("jump", False)),
                status=r.get("status", "pending"),
                paid=bool(r.get("paid", False)),
                payment_method=r.get("payment_method", "free"),
                price=float(r.get("price", 0.0)),
                created_at=r.get("created_at") or time.time(),
                approved_at=r.get("approved_at") or 0.0,
                resolved_at=r.get("resolved_at") or 0.0,
            )

    new_requests = [
        {
            "id": r.id,
            "song_ids": r.song_ids,
            "song_titles": r.song_titles,
            "requester_name": r.requester_name,
            "jump": r.jump,
            "paid": r.paid,
            "payment_method": r.payment_method,
            "price": r.price,
        }
        for r in bar.requests.values()
        if r.status == "pending"
    ]

    actions = list(bar.pending_actions)
    bar.pending_actions.clear()

    reports_needed = _reconcile_reports(bar, body.get("report_filenames"))

    return {
        "requests": new_requests,
        "actions": actions,
        "desired_settings": dict(bar.desired_settings),
        "reports_needed": reports_needed,
    }


@app.post("/api/host/report_upload")
async def host_report_upload(body: dict[str, Any]):
    """
    Event-driven, not part of the regular 5s sync — called once by the host right after it
    actually generates a new local report file, and again for any filename the relay names in a
    register/sync response's `reports_needed` (backfill after a relay restart, a genuine new
    session, or a previously-dropped upload). Kept separate from host_sync()'s payload since CSV
    content can be nontrivially sized and most ticks have nothing new to send.
    """
    jukebar_id = body.get("jukebar_id", "")
    session    = body.get("session", "")
    filename   = body.get("filename", "")
    content    = body.get("content", "")
    bar = _get_bar(jukebar_id)
    _validate_session(bar, session)
    if not filename:
        raise HTTPException(400, "filename required")
    bar.reports[filename] = {
        "content": content,
        "created_at": body.get("created_at") or time.time(),
    }
    return {"ok": True}


# ---------------------------------------------------------------------------
# Customer endpoints (internet mode)
# ---------------------------------------------------------------------------

def _customer_bar(jukebar_id: str, session: str) -> BarSession:
    bar = _get_bar(jukebar_id)
    _validate_session(bar, session)
    _touch(bar)
    return bar


def _require_bartender_token(bar: BarSession, token: str) -> None:
    """Raise 401 unless token is a currently-valid bartender/admin token minted by
    bar_authenticate(). Call this in every bartender/admin action endpoint — approve, deny,
    control, settings, requests, history — none of these should be reachable with just the
    session token customers also have (see BarSession.bartender_tokens docstring)."""
    if not token or token not in bar.bartender_tokens:
        raise HTTPException(401, "Bartender/admin authentication required")


def _require_admin_token(bar: BarSession, token: str) -> None:
    """Stricter than _require_bartender_token: also requires the token's role to be "admin" —
    for the Bartender Sessions view/kill/clear-lockout actions and bartender-PIN changes, none of
    which a bartender should be able to do to itself or another bartender. Tokens minted before
    the 2026-08 role split (none should exist in a live process, but defensively) have no "role"
    key at all and are treated as non-admin."""
    _require_bartender_token(bar, token)
    if bar.bartender_tokens[token].get("role") != "admin":
        raise HTTPException(403, "Admin authentication required")


_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/bar/{jukebar_id}", response_class=HTMLResponse)
async def bar_page(jukebar_id: str):
    # Hard lockout (real 404, not just unadvertised) when the bar has locked itself to
    # kiosk_mode "localOnly" — the page shouldn't even load, not just fail to submit once loaded.
    bar = _bars.get(jukebar_id)
    if bar is not None and bar.kiosk_mode == "localOnly":
        raise HTTPException(404, "Not found")
    return HTMLResponse((Path("static") / "customer.html").read_text(encoding="utf-8"), headers=_NO_CACHE)


_BARTENDER_OFF_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>JukeBar</title>
<style>
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#1a1a1a; color:#eee; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         text-align:center; padding:24px; box-sizing:border-box; }
  .card { max-width:360px; }
  h1 { color:#fda185; font-size:1.3em; margin:0 0 12px; }
  p { color:#bbb; line-height:1.5; margin:0; }
</style></head>
<body>
  <div class="card">
    <h1>Bartender access unavailable</h1>
    <p>Bartender access for this bar is currently turned off, or your session was ended by the
       admin. Please check with bar staff.</p>
  </div>
</body></html>"""


@app.get("/bartender/{jukebar_id}", response_class=HTMLResponse)
async def bartender_page(jukebar_id: str):
    # Bartender role is off entirely for this bar when no bartender PIN has been set (2026-08
    # PIN split) — real 404, not just an unadvertised link, mirroring bar_page()'s localOnly gate.
    # Returns a friendly HTML page (not a bare JSON 404) since this route legitimately gets hit
    # by a previously-paired bartender's browser too — a stale tab reload, or reopening after the
    # admin turns access off underneath them (2026-08-18: was raising a bare HTTPException(404),
    # showing raw `{"detail":"Not found"}` JSON with zero explanation, reported as confusing).
    bar = _bars.get(jukebar_id)
    if bar is not None and not bar.bartender_pin_hash:
        return HTMLResponse(_BARTENDER_OFF_HTML, status_code=404, headers=_NO_CACHE)
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
            # Effective, not raw: customer.html should hide/disable its own Request button while
            # paused, not just get a 403 after tapping it — see BarSession.effective_accepting_requests.
            "accepting_requests":     bar.effective_accepting_requests,
            "kiosk_mode":             bar.kiosk_mode,
            "settings_pending":       list(bar.desired_settings.keys()),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/bar/{jukebar_id}/authenticate")
async def bar_authenticate(jukebar_id: str, request: Request, s: str = Query(..., alias="s"), body: dict[str, Any] = ...):
    """
    Bartender/admin PIN gate. Client hashes the PIN with SHA-256 (same algorithm as iOS CryptoKit)
    and posts the hex digest, plus a "role" of "admin" or "bartender" (admin.html/bartender.html
    send their own role; role defaults to "admin" for any older client that predates the split).
    On success, mints and returns a bartender token that must be sent on every subsequent
    bartender/admin action (approve/deny/control/settings/requests/history) —
    see _require_bartender_token(). Returns 403 Forbidden on a wrong PIN.

    admin and bartender check two independent secrets (bar.pin_hash / bar.bartender_pin_hash,
    split 2026-08 — previously one shared PIN). If the bar has no pin_hash at all, admin access
    is denied (PIN is mandatory for admin). If bar.bartender_pin_hash is empty, the bartender
    role is off for this bar entirely — 404, not 403, so bartender.html's "session ended or bar
    offline" messaging fires rather than implying a PIN was just typed wrong.

    Locked out per (bar, source IP, role) after BARTENDER_LOCKOUT_MAX_ATTEMPTS wrong guesses, for
    BARTENDER_LOCKOUT_SECONDS — other IPs/bartenders/the admin PIN are unaffected.
    """
    bar = _customer_bar(jukebar_id, s)
    role = body.get("role") or "admin"
    if role not in ("admin", "bartender"):
        raise HTTPException(400, "role must be admin or bartender")
    client_ip = request.client.host if request.client else "unknown"
    key = (jukebar_id, client_ip, role)
    now = time.time()
    entry = _bartender_lockouts.get(key)
    if entry and now < entry["locked_until"]:
        remaining = int(entry["locked_until"] - now)
        raise HTTPException(429, f"Too many attempts — try again in {remaining}s")

    expected_hash = bar.pin_hash if role == "admin" else bar.bartender_pin_hash
    if not expected_hash:
        if role == "bartender":
            raise HTTPException(404, "Bartender access is not enabled for this bar")
        raise HTTPException(403, "No PIN configured for this bar")
    if body.get("pin_hash") != expected_hash:
        attempts = (entry["attempts"] if entry else 0) + 1
        locked_until = now + BARTENDER_LOCKOUT_SECONDS if attempts >= BARTENDER_LOCKOUT_MAX_ATTEMPTS else 0
        # last_name: whatever name (if any) was typed alongside this failed attempt — purely
        # informational, shown on the Bartender Sessions admin tab so an admin clearing a lockout
        # has a hint who's asking, not used for anything security-relevant.
        _bartender_lockouts[key] = {
            "attempts": attempts, "locked_until": locked_until,
            "last_name": (body.get("name") or "").strip()[:60],
        }
        raise HTTPException(403, "Incorrect PIN")

    _bartender_lockouts.pop(key, None)
    raw_name = (body.get("name") or "").strip()
    # Name is required for bartender logins, minimum 2 characters (2026-08-18) — previously a
    # blank name silently defaulted to the literal "Bartender", defeating the entire point of
    # collecting one (telling multiple bartenders apart on the Sessions tab / Kill action). Admin
    # has no name field/concept at all, so this is scoped to role == "bartender" only.
    if role == "bartender" and len(raw_name) < 2:
        raise HTTPException(400, "Name must be at least 2 characters")
    final_name = raw_name[:60]
    # Bartender names must be unique among currently-active bartender sessions for this bar
    # (2026-08-18) — otherwise the Sessions tab's Kill action can't reliably target the right
    # person once two people share a name, defeating the whole point of collecting one. Only
    # checked against *active* sessions (bartender_tokens entries are deleted outright on Kill —
    # see bartender_sessions_kill()), so a freed-up name becomes available again immediately.
    # Scoped to role == "bartender" only — admin doesn't have this ambiguity concern.
    if role == "bartender" and any(
        rec.get("role") == "bartender" and rec.get("name", "").strip().lower() == final_name.lower()
        for rec in bar.bartender_tokens.values()
    ):
        raise HTTPException(409, f'The name "{final_name}" is already in use by an active bartender — pick a different name.')
    token = uuid.uuid4().hex
    bar.bartender_tokens[token] = {
        "name":       final_name,
        "paired_at":  now,
        "ip":         client_ip,
        "role":       role,
        # Separate opaque id for the Bartender Sessions admin UI (2026-08) — admin.html lists/kills
        # sessions by this, never by the actual bearer token, so the working credential is never
        # round-tripped back through a view a bartender could conceivably also load.
        "session_id": uuid.uuid4().hex,
    }
    return {"ok": True, "token": token}


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
    return {
        "now_playing": bar.now_playing,
        "is_playing": bar.is_playing,
        "spotify_outage_active": bar.spotify_outage_active,
    }


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


def _compute_price(bar: "BarSession", song_ids: list[str]) -> float:
    pps = bar.price_per_song
    p3  = bar.price_for_three
    if pps <= 0:
        return 0.0
    return p3 if (len(song_ids) == 3 and p3 > 0) else pps * len(song_ids)


def _requester_outstanding_count(bar: "BarSession", ip: str, customer_id: str) -> int:
    """See MAX_OUTSTANDING_REQUESTS_PER_REQUESTER's comment for the union-of-two-signals design."""
    count = 0
    for r in bar.requests.values():
        if r.status in ("played", "denied", "unfulfilled"):
            continue
        if (ip and r.requester_ip == ip) or (customer_id and r.customer_id == customer_id):
            count += 1
    return count


@app.post("/api/bar/{jukebar_id}/request")
async def bar_request(jukebar_id: str, request: Request, s: str = Query(..., alias="s"), body: dict[str, Any] = ...):
    bar = _customer_bar(jukebar_id, s)
    if bar.kiosk_mode == "localOnly":
        raise HTTPException(404, "Not found")
    if not bar.effective_accepting_requests:
        raise HTTPException(403, "Not accepting requests right now")

    song_ids: list[str] = body.get("song_ids", [])
    if not song_ids:
        raise HTTPException(400, "song_ids required")

    client_ip = request.client.host if request.client else ""
    customer_id = body.get("customer_id", "")
    if _requester_outstanding_count(bar, client_ip, customer_id) > MAX_OUTSTANDING_REQUESTS_PER_REQUESTER:
        raise HTTPException(429, "You already have requests waiting to be reviewed — please wait before submitting more")

    rid = str(uuid.uuid4())
    req = SongRequest(
        id=rid,
        song_ids=song_ids,
        song_titles=body.get("song_titles", []),
        requester_name=body.get("requester_name", ""),
        customer_id=customer_id,
        jump=body.get("jump", False),
        status="pending",  # always pending; host confirms via up_next — relay is not the authority
        # This endpoint ("Submit to Bartender") is only ever reached for the free/pay-to-bartender
        # path, never Stripe (that's create-payment-intent/payment-confirmed) - so bartender_enabled
        # is the only signal that determines whether this request is actually charged. A free/
        # auto-accept request is never charged, regardless of whatever price_per_song/
        # price_for_three happen to still be configured.
        price=_compute_price(bar, song_ids) if bar.bartender_enabled else 0.0,
        requester_ip=client_ip,
    )
    bar.requests[rid] = req
    return {"request_id": rid, "status": req.status}


@app.get("/api/bar/{jukebar_id}/request/{request_id}")
async def bar_request_status(jukebar_id: str, request_id: str, s: str = Query(..., alias="s")):
    bar = _customer_bar(jukebar_id, s)
    if bar.kiosk_mode == "localOnly":
        raise HTTPException(404, "Not found")
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
    if bar.kiosk_mode == "localOnly":
        raise HTTPException(404, "Not found")
    if not bar.effective_accepting_requests:
        raise HTTPException(403, "Not accepting requests right now")
    if not bar.stripe_secret_key:
        raise HTTPException(400, "Stripe not configured")
    song_ids: list[str] = body.get("song_ids", [])
    if not song_ids:
        raise HTTPException(400, "song_ids required")
    price = _compute_price(bar, song_ids)
    if price <= 0:
        raise HTTPException(400, "No price configured")
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
    if bar.kiosk_mode == "localOnly":
        raise HTTPException(404, "Not found")
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
        price=_compute_price(bar, song_ids),
    )
    bar.requests[rid] = req
    return {"request_id": rid, "status": "approved"}


# ---------------------------------------------------------------------------
# Bartender endpoints (internet mode)
# ---------------------------------------------------------------------------

@app.get("/api/bar/{jukebar_id}/requests")
async def bartender_requests(jukebar_id: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token")):
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
    _require_bartender_token(bar, token)
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
            "price": r.price,
            "created_at": r.created_at,
            "approved_at": r.approved_at,
            "resolved_at": r.resolved_at,
        }
        for r in sorted(bar.requests.values(), key=lambda r: r.created_at)
        if r.status in ("pending", "approved", "approved_jump")
    ]
    return {"bar_name": bar.bar_name, "requests": pending, "now_playing": bar.now_playing,
            "price_per_song": bar.price_per_song, "price_for_three": bar.price_for_three,
            "currency": bar.currency, "require_approval": bar.require_approval,
            "stripe_enabled": bar.stripe_enabled, "bartender_enabled": bar.bartender_enabled,
            "accepting_requests": bar.accepting_requests, "kiosk_mode": bar.kiosk_mode,
            "bartender_access_enabled": bool(bar.bartender_pin_hash),
            "auto_manage_requests": bar.auto_manage_requests,
            "auto_manage_max": bar.auto_manage_max,
            "auto_manage_restart": bar.auto_manage_restart,
            "settings_pending": list(bar.desired_settings.keys())}


@app.post("/api/bar/{jukebar_id}/approve")
async def bartender_approve(jukebar_id: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token"), body: dict[str, Any] = ...):
    bar = _customer_bar(jukebar_id, s)
    _require_bartender_token(bar, token)
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
async def bar_history(jukebar_id: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token")):
    """
    All requests for the current session, any status - powers admin page's Reports tab (status
    counts + full request list). Shaped the same as bartender_requests()'s entries (song_details,
    payment_method, price, etc.) so the client can reuse the same requestCard() renderer for both.
    """
    bar = _customer_bar(jukebar_id, s)
    _require_bartender_token(bar, token)
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
                "price": r.price,
                "created_at": r.created_at,
                "approved_at": r.approved_at,
            }
            for r in sorted(bar.requests.values(), key=lambda r: r.created_at)
        ],
    }


@app.post("/api/bar/{jukebar_id}/control")
async def bartender_control(jukebar_id: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token"), body: dict[str, Any] = ...):
    bar = _customer_bar(jukebar_id, s)
    _require_bartender_token(bar, token)
    action = body.get("action", "")
    if action not in ("play", "pause", "next", "prev"):
        raise HTTPException(400, "action must be play, pause, next, or prev")
    # Reject remote Play while Spotify is disconnected (2026-08-08) — resuming without actually
    # reconnecting at the kiosk first would just immediately re-trip the same outage on the next
    # Spotify song. The host enforces this too (belt and suspenders), but rejecting here also
    # skips the optimistic is_playing flip below, so admin.html doesn't show a misleading
    # "now playing" state for the ~5s until the host's own echo corrects it.
    if action == "play" and bar.spotify_outage_active:
        raise HTTPException(409, "Spotify is disconnected — reconnect at the kiosk first")
    bar.pending_actions.append({"type": "control", "action": action})
    # Optimistically update is_playing so fetchNowPlaying reflects the new state
    # immediately — before Android picks up the action on its next sync cycle.
    if action == "pause":
        bar.is_playing = False
    elif action == "play":
        bar.is_playing = True
    return {"ok": True}


@app.post("/api/bar/{jukebar_id}/deny")
async def bartender_deny(jukebar_id: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token"), body: dict[str, Any] = ...):
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
    _require_bartender_token(bar, token)
    rid = body.get("request_id", "")
    req = bar.requests.get(rid)
    if req is None:
        raise HTTPException(404, "Request not found")
    if req.payment_method != "free":
        raise HTTPException(403, "Paid requests cannot be denied or cancelled")
    req.status = "denied"
    req.resolved_at = time.time()
    bar.pending_actions.append({
        "type": "deny",
        "request_id": rid,
    })
    return {"ok": True}


@app.post("/api/bar/{jukebar_id}/settings")
async def bar_settings(jukebar_id: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token"), body: dict[str, Any] = ...):
    """
    Admin/bartender: record the desired value for one or more settings fields. Does NOT touch
    bar.bartender_enabled/stripe_enabled/accepting_requests/bartender_pin_hash directly - the
    host picks up desired_settings on its next /api/host/sync call, applies it locally, and its
    own echoed current value is what actually updates the field (see host_sync()). A newer
    request for the same field before the host catches up just overwrites the desired value in
    place - only the latest matters, nothing is queued or ordered.

    bartender_pin_hash and the three auto-manage-requests fields require an admin token
    specifically (2026-08, Bartender Sessions work; 2026-08-28, auto-manage — neither is exposed
    on bartender.html, admin-only by UI convention there too) — unlike the three toggle fields
    below, which any valid bartender token can still set, same as always.
    """
    bar = _customer_bar(jukebar_id, s)
    _require_bartender_token(bar, token)
    for field_name in ("bartender_enabled", "stripe_enabled", "accepting_requests"):
        if field_name in body:
            bar.desired_settings[field_name] = bool(body[field_name])
    if "bartender_pin_hash" in body:
        _require_admin_token(bar, token)
        bar.desired_settings["bartender_pin_hash"] = str(body["bartender_pin_hash"] or "")
    if "auto_manage_requests" in body:
        _require_admin_token(bar, token)
        bar.desired_settings["auto_manage_requests"] = bool(body["auto_manage_requests"])
    for field_name in ("auto_manage_max", "auto_manage_restart"):
        if field_name in body:
            _require_admin_token(bar, token)
            bar.desired_settings[field_name] = int(body[field_name])
    return {"ok": True}


@app.get("/api/bar/{jukebar_id}/bartender_sessions")
async def bartender_sessions(jukebar_id: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token")):
    """
    Admin-only (2026-08, Bartender Sessions tab). Lists currently-paired bartender tokens and
    currently-tracked PIN-lockout entries for THIS bar — both already existed as bookkeeping for
    other purposes (bartender_tokens for _require_bartender_token(), _bartender_lockouts for
    bar_authenticate()'s rate limiting) and are just being surfaced here, not new state.

    Deliberately returns "session_id" (opaque, minted alongside the real token at pairing time),
    never the bartender's actual bearer token — admin.html can list/kill sessions without the
    admin's browser ever holding a working bartender credential.
    """
    bar = _customer_bar(jukebar_id, s)
    _require_admin_token(bar, token)
    now = time.time()
    sessions = sorted(
        (
            {
                "session_id": rec["session_id"],
                "name":       rec["name"],
                "paired_at":  rec["paired_at"],
                "ip":         rec["ip"],
            }
            for rec in bar.bartender_tokens.values()
            if rec.get("role") == "bartender"
        ),
        # Earliest sign-in first, latest last (2026-08-17, was reverse=True/latest-first) — the
        # earliest session for a given name is the presumed-legitimate one; later duplicates
        # (same name signing in again, or an impostor) show up at the bottom where they stand out
        # against the established order rather than jumping to the top.
        key=lambda r: r["paired_at"],
    )
    lockouts = sorted(
        (
            {
                "ip":            ip,
                "attempts":      entry["attempts"],
                "locked_until":  entry["locked_until"],
                "locked":        now < entry["locked_until"],
                "last_name":     entry.get("last_name", ""),
            }
            for (bid, ip, role), entry in _bartender_lockouts.items()
            if bid == jukebar_id and role == "bartender" and entry.get("attempts", 0) > 0
        ),
        key=lambda r: r["locked_until"], reverse=True,
    )
    return {"sessions": sessions, "lockouts": lockouts}


@app.get("/api/bar/{jukebar_id}/bartender_qr.png")
async def bar_bartender_qr(jukebar_id: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token")):
    """
    Bartender QR image for render admin.html's Sessions tab (2026-08-17). Before this, only
    kiosk-native (iOS QRImageView / Android generateQrBitmap) actually drew a QR image — render
    and LAN admin.html showed status text only, with no image and no visible link at all, so an
    admin using either of those had no way to get the bartender URL except walking to the kiosk's
    own screen. Admin-token-gated, same reasoning as the report endpoints (this reveals a live,
    working login URL, not just informational text).
    """
    bar = _customer_bar(jukebar_id, s)
    _require_admin_token(bar, token)
    if not bar.bartender_pin_hash:
        raise HTTPException(404, "Bartender access not enabled")
    url = f"https://jukebars.com/bartender/{jukebar_id}?s={bar.session}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png", headers=_NO_CACHE)


@app.post("/api/bar/{jukebar_id}/bartender_sessions/kill")
async def bartender_sessions_kill(jukebar_id: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token"), body: dict[str, Any] = ...):
    """Admin-only. Revokes one bartender's token immediately — their next call to any
    bartender/admin action endpoint gets 401 (see _require_bartender_token()). Does not touch
    the bartender PIN itself; pair with bar_settings()'s bartender_pin_hash if the admin also
    wants to stop this device from simply re-pairing with the old PIN (that's a second, separate
    call from the client - see admin.html's kill-session confirm flow)."""
    bar = _customer_bar(jukebar_id, s)
    _require_admin_token(bar, token)
    session_id = body.get("session_id", "")
    match = next(
        (tok for tok, rec in bar.bartender_tokens.items()
         if rec.get("session_id") == session_id and rec.get("role") == "bartender"),
        None,
    )
    if match is None:
        raise HTTPException(404, "Session not found")
    del bar.bartender_tokens[match]
    return {"ok": True}


@app.post("/api/bar/{jukebar_id}/bartender_sessions/clear_lockout")
async def bartender_sessions_clear_lockout(jukebar_id: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token"), body: dict[str, Any] = ...):
    """Admin-only. Resets one IP's failed-attempt counter for bartender pairing (not admin PIN
    lockouts, which are a separate lockout entirely and not exposed here) — lets a legitimately
    locked-out bartender retry immediately once told the (unchanged) current PIN verbally."""
    bar = _customer_bar(jukebar_id, s)
    _require_admin_token(bar, token)
    ip = body.get("ip", "")
    _bartender_lockouts.pop((jukebar_id, ip, "bartender"), None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Report mirror (2026-08-07) — admin-only. bar.reports mirrors whatever the host currently has
# locally (see _reconcile_reports()); the relay never generates or interprets report content
# itself, purely a pass-through so render admin.html can list/download/trigger-generation without
# needing LAN/physical access to the kiosk. Gated by _require_admin_token (not the looser
# _require_bartender_token) since these are financial/accounting records — stricter than the three
# payment toggles bartender.html can already touch.
# ---------------------------------------------------------------------------

@app.get("/api/bar/{jukebar_id}/reports")
async def bar_reports_list(jukebar_id: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token")):
    bar = _customer_bar(jukebar_id, s)
    _require_admin_token(bar, token)
    reports = sorted(
        (
            {"filename": name, "created_at": r["created_at"], "size": len(r["content"])}
            for name, r in bar.reports.items()
        ),
        key=lambda r: r["created_at"], reverse=True,
    )
    return {"reports": reports}


@app.get("/api/bar/{jukebar_id}/reports/{filename}")
async def bar_reports_download(jukebar_id: str, filename: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token")):
    """
    Downloading a report IS the cleanup action — no separate delete endpoint. On success, removes
    it from the relay's mirror immediately and queues a delete_report action for the host to remove
    its own local copy on the next sync, per the user's explicit design: "after a client admin
    downloaded the report, the system needs to be cleaned."
    """
    bar = _customer_bar(jukebar_id, s)
    _require_admin_token(bar, token)
    report = bar.reports.get(filename)
    if report is None:
        raise HTTPException(404, "Report not found")
    del bar.reports[filename]
    bar.pending_actions.append({"type": "delete_report", "filename": filename})
    return Response(
        content=report["content"],
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/bar/{jukebar_id}/reports/generate")
async def bar_reports_generate(jukebar_id: str, s: str = Query(..., alias="s"), token: str = Query(..., alias="token")):
    """Admin-only. Queues a generate_report action; the host applies it on its next sync (builds
    the report synchronously, wipes played/denied requests locally, uploads the result) — same
    up-to-~5s latency as every other relay-mediated action, nothing special-cased for this one."""
    bar = _customer_bar(jukebar_id, s)
    _require_admin_token(bar, token)
    bar.pending_actions.append({"type": "generate_report"})
    return {"ok": True}
