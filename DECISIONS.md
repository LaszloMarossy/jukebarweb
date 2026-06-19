# jukebarweb — Architecture Decisions Log

jukebarweb is the shared cloud relay backend for the JukeBar product family. It runs on Render (free tier) and is the only internet-facing component. All client apps (iOS JukeBar, Android SpotOnJukeBar) share it unchanged.

> SPEC is implicit — see the docstring at the top of `main.py` for the full API surface. This file captures decisions and the session log.

---

## Client apps that use this relay

| App | Platform | Playback source | Status |
|---|---|---|---|
| JukeBar | iOS (Swift/SwiftUI) | Apple Music / MusicKit | Active |
| SpotOnJukeBar | Android (Kotlin/Compose) | Spotify + local MP3s | In development |

Both clients speak the same relay API. No relay code changes are needed for SpotOnJukeBar — it is a drop-in second client.

---

## Decision: Render free tier — memory-only BarSessions, GCS-persisted MapEntries

`BarSession` objects (catalog, requests, pending actions) are held in memory only — iOS re-registers within 5 s of any restart so memory-only is safe. `MapEntry` records (discover map) are persisted to GCS so they survive Render cold starts and instance recycling. Falls back to local disk when `GCS_BUCKET` env var is absent (local dev).

---

## Decision: Single relay serves all connection modes

The relay has no concept of "which client app" is connected. Both JukeBar (iOS) and SpotOnJukeBar (Android) register via `POST /api/host/register` with the same fields. The relay is client-agnostic.

---

## Decision: Map / discover layer is always-on, relay sessions are opt-in

`POST /api/map/register` is called by all bars regardless of connection mode (WiFi, hotspot, internet, local). The discover map shows all registered bars. `POST /api/host/register` is only called in internet mode and creates a live `BarSession` for relay traffic.

---

## Decision: Cleanup intervals

- `BAR_TIMEOUT_SECONDS = 300` — bar shown as offline on the map after 5 min without a sync
- `BAR_CLEANUP_INACTIVE = 7200` — BarSession eligible for cleanup after 2 h without sync
- `BAR_CLEANUP_MIN_AGE = 1800` — session must also be at least 30 min old before sweep
- `CLEANUP_INTERVAL = 300` — sweep runs every 5 min

---

## Session log

---

### 2026-05-23 — SpotOnJukeBar Android project created; relay confirmed as shared backend

**Context:** Decision made to build an Android sibling app (SpotOnJukeBar) using Spotify + local MP3 files as the playback engine. Apple Music / MusicKit are iOS-only; Android has no equivalent SDK.

**Key decisions made (documented in SpotOnJukeBar/DECISIONS.md):**
- Spotify Android SDK as primary shuffle engine (streaming, requires Premium)
- ExoPlayer for local MP3 file playback
- Coordinator layer manages handoff between the two players at track boundaries
- Spotify app stays in permanent warm-pause state in background — no cold-start overhead per handoff
- Development on Spotify Personal Premium; switch to Soundtrack by Spotify for commercial bar use

**Impact on jukebarweb:** None. The relay API is identical for both clients. SpotOnJukeBar will call the same endpoints (`/api/host/register`, `/api/host/sync`, `/api/map/register`, `/api/bar/{id}/*`) with no changes needed server-side.

**SpotOnJukeBar project location:** `/Users/laszlo/dev/giffy/spotonjukebar`
**GitHub:** https://github.com/LaszloMarossy/spotonjukebar

---

### 2026-05-23 — iOS bug fix: "Starting up" overlay stuck in internet/local mode

**iOS JukeBar bug (fixed in commit 502e7e3):** After completing setup in internet-only or local mode, `beginSetup()` set `showAdminAfterSetup = true` before `isServerReady` was true. KioskView's overlay waited forever because no local Swifter server starts in those modes.

**Fix:** Set `isServerReady = true` before `showAdminAfterSetup = true` for modes that don't use the local server.

**Impact on jukebarweb:** None.

---

### 2026-06-01 — approved_request_ids sync field; Android map pins

**Context:** SpotOnJukeBar Android app wired up the relay sync loop.

**`approved_request_ids` field added to `/api/host/sync`:**
Previously only `played_request_ids` was sent. Android sends `approved_request_ids` as a separate list so the server can set request status to `"approved"` (with `approved_at` timestamp) immediately when the bartender approves, before the song actually starts playing. This keeps the admin HTML "Up Next" section populated. The server processes `approved_request_ids` in `host_sync` by setting `status = "approved"` for any pending request whose ID appears in the list.

**Android map pins:**
iOS already sent `lat`/`lng` from `CLLocationManager`. Android was sending only a text `location` string, so entries had `lat=None` and were filtered out by `discover.html` (`b.lat != null && b.lng != null`). Fixed: Android now sends `lat`/`lng` via `LocationManager.getLastKnownLocation()`. No server-side change needed — the endpoint already accepted and stored coordinates.

---

### 2026-05-23 — playlistNote and playlistDisplayName added to map registration

**iOS JukeBar change (commit 502e7e3):** Two new optional fields added to `BarConfig` and `registerOnMap()`:
- `playlist_note` — DJ's short note (e.g. "every Thursday night!")
- `playlist_display_name` — human-friendly name override for the playlist

**jukebarweb change required:** `POST /api/map/register` already accepts and stores these fields via the `playlists` array (`note`, `display_name` keys). The `map_register` endpoint was already updated in a previous session to handle per-playlist metadata. No further server changes needed.

---

### 2026-06-19 — Per-artist song count weighting in genre profiling

**Context:** The profiling daemon was treating every artist as equally important (song_count=1), regardless of how many tracks they appear on in the playlist. A bar where The Cure plays 25 times looked the same as one where they appear once.

**Change:** iOS (`AppState.swift`) and Android (`RelayClient.kt` + `MainActivity.kt`) now count tracks per artist and send `[{name, song_count}]` instead of `[String]` to `POST /api/map/register`. The relay normalises legacy format on the way in. The daemon unpacks the count and passes it to `resolve_artist()` which multiplies tag weights by `song_count` — so an artist with 20 plays contributes 20× more to the pie.

**Backward compat:** Relay accepts the old `[String]` format and converts it to `[{name, song_count: 1}]` so un-updated clients keep working.

**Logging added:** Daemon prints top-5 artists by song count per playlist so the weighting effect is visible in the log.

---

### 2026-06-01 — Map clustering; map persistence policy confirmed

**Map clustering (`discover.html`):**
Added Leaflet.markercluster (1.5.3 via CDN). Nearby bars now collapse into a single cluster pin showing the count; clicking zooms in and eventually spiderfies individual bar markers. Cluster icons use the grapefruit brand colour (`--accent: #fda185`) with a count badge. Single bars continue to use the existing `jb-marker` `divIcon`.

**Map persistence policy (Android fix):**
Android was calling `unregisterMap` in both `restartSession()` and `resetSetup()`, causing bars to vanish from the map on every app restart. Removed those calls — `MapEntry` records are now long-lived on the server (GCS-persisted). A bar shows as dormant with its last-active date after the 5-min `BAR_TIMEOUT_SECONDS` expires. This matches iOS behaviour. `unregisterMap` should only be called when the user explicitly disables the map listing in the wizard.
