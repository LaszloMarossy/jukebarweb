# JukeBarSpot — Product Specification

> **Architecture is in DECISIONS.md.** This file covers product behaviour (what it does); DECISIONS.md covers implementation (how and why).
> JukeBarSpot is the Android sibling of JukeBar (iOS). The product behaviour is identical — see JukeBar SPEC.md for the full spec. This file captures Android-specific differences only.

---

## Overview

JukeBarSpot is JukeBar for Android. Same concept: a self-contained jukebox running on one Android device, serving customer and bartender web apps over the local WiFi. The key difference from the iOS version is the playback engine: Spotify (streaming, requires subscription) as the primary shuffle source, with local MP3 files as a secondary injection layer. Both sources are presented to customers as a single unified catalog.

---

## What's the same as JukeBar (iOS)

- Customer web app (customer.html) — reused as-is
- Bartender web app (bartender.html) — reused as-is
- Admin web app (admin.html) — reused as-is
- Render relay backend (jukebarweb) — reused as-is
- Request / approval flow
- Session lifecycle (playlist_id rotation, Stop button, 30-min pause timer)
- Bartender pairing via PIN
- Discover map registration
- JSON file storage structure in app's Documents equivalent
- Session CSV reports

---

## What's different

### Playback engine
- **Primary shuffle source:** Spotify playlist (streaming via Spotify Android SDK)
- **Secondary layer:** local MP3 files uploaded to the device, injected at track boundaries
- Both sources merged into a single unified catalog presented to customers
- Spotify app runs in background in a permanent warm-pause state; local files play via ExoPlayer

### Platform
- Language: Kotlin
- UI: Jetpack Compose
- Embedded HTTP server: NanoHTTPD or Ktor (port 8080)
- Media library: Spotify Android SDK + ExoPlayer for local files
- Storage: same JSON file pattern in app's external files dir

### No Apple Music, no MusicKit
- Spotify is the streaming source; Apple Music is not available on Android via SDK
- Bar owners need a Spotify Premium subscription on the kiosk device

---

## System Surfaces

1. **SpotOnJukeBar** — Android app (Kotlin/Compose). Runs on the bar's Android phone/tablet.
2. **Customer Web App** — reused from JukeBar, served by the embedded HTTP server (WiFi/hotspot modes) or by jukebarweb relay (internet mode)
3. **Bartender Web App** — reused from JukeBar, same delivery as above
4. **Admin Web App** — reused from JukeBar, same delivery
5. **Embedded HTTP Server** — NanoHTTPD on port 8080 (WiFi/hotspot modes)
6. **jukebarweb relay** — Render.com cloud relay for internet mode; shared with iOS JukeBar

---

## Session lifecycle

1. Staff complete the setup wizard (display mode, network mode, folder, Spotify playlist, bar name, PIN, approval mode, pricing)
2. At the SUMMARY step the queue is built and playback starts; the admin page is shown
3. Staff tap "Done" → kiosk view becomes active, screen stays on
4. Long-press the JukeBar logo → PIN entry → admin overlay (same admin page)
5. "End Session" → confirmation → wizard restarts from step 1 with all previous values pre-filled; Spotify auth is silently refreshed in the background

---

## Patron request flow

1. Customer scans QR code → customer web app → selects songs → submits request
2. If `requireApproval = true`: request appears in admin overlay pending queue; staff approve/deny
3. If `requireApproval = false`: request auto-accepted
4. Approved songs inserted after last pending requested song in queue (near front, not end)
5. "Play Next" inserts immediately after current song without interrupting it
6. Admin HTML "Up Next" remains visible until the requested song actually starts playing

---

## Internet mode relay

- Android registers with jukebarweb at `POST /api/host/register` with full catalog
- Sync loop runs every 5 s: sends played/approved request IDs, receives new requests and actions
- nowPlaying push within 2 s of any change; heartbeat every 30 s
- Web admin at `jukebars.com/admin/{jukebarId}` supports approve/deny/control/stop_session
- Session re-registers automatically after Render cold starts (404 triggers re-register)

---

## Discover map

- Opt-in per session; controlled by `listOnMap` in bar settings
- Registers at `POST /api/map/register` with bar name, location text, playlist info, artist list, and GPS coordinates (lat/lng from device location)
- Unregisters on End Session / Stop Session
- Bar appears on `jukebars.com` discover map; requires `ACCESS_COARSE_LOCATION` permission

---

## Deferred / Out of Scope (initial version)

- Local-only mode (no Spotify) — ExoPlayer-only path possible, deferred
- Offline playback — Spotify requires internet; local files work offline but shuffle does not
- Apple Music — not available on Android
- Session CSV reports — not yet implemented
- Customer request flow from the kiosk device itself (Request button is a placeholder)
