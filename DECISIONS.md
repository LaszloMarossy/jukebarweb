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

### 2026-06-29 — Hotspot/WiFi mode: offline operation and limitations

**Advantage — works with zero internet:**
When the iOS host device has no WiFi and no cellular, it can open a personal hotspot and run JukeBar entirely offline, provided Apple Music songs are downloaded to the device. The local HTTP server binds to the hotspot IP (typically `172.20.10.1`); customers and the bartender connect over that LAN. MusicKit plays downloaded tracks without internet. Admin actions take effect immediately with no relay round-trip — this is a concrete latency and reliability advantage over internet mode.

Android + Spotify does **not** work offline: the Spotify App Remote SDK requires an authenticated Spotify session and cannot play tracks (even downloaded ones) without internet. A future local-file mode (MediaStore + ExoPlayer) would remove this constraint.

**Limitations in no-internet hotspot mode:**
- Stripe payments cannot process (requires internet to reach Stripe's API). Use auto-approve or pay-to-bartender instead.
- The relay (`jukebarweb` on Render) is unreachable — internet mode is unavailable, but LAN mode needs no relay.
- **The option to list the bar on the community page at jukebars.com will not work** — map registration (`POST /api/map/register`) requires the relay, which is internet-only.

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

---

### 2026-07-10 — architecture.html: resolved `@todo` review comments

**Context:** User dropped ~20 `@todo` comments into `docs/architecture.html` (route inventory, flow diagrams) asking "who calls this, for what, what does it do" for handlers whose purpose wasn't obvious from the diagram alone.

**Resolved by reading `main.py`, iOS `LocalServer.swift`/`AppState.swift`/`AdminView.swift`, and the `static/*.html` fetch call sites:** filled in caller + purpose for every `/api/bar/{id}/*` route and the iOS `LocalServer.swift` route column (which previously had bare function names while the Android column already had descriptions); expanded flow A step 3 into an explicit two-way payload list for `/api/host/sync`; clarified that flow C (free/pay-to-bartender) and flow D (Stripe/CC) are disjoint paths — a paid request never touches the pending-approval queue described in flow C step 2; clarified that `host_register()` (flow A) and `map_register()` (flow E, community map opt-in) are independent registrations that happen to both carry "playlist" data, which was the source of one mix-up.

**Answered live, not written into the doc (per explicit instruction not to change that passage):** yes, the relay's optimistic-apply in `bar_settings()` — patching `BarSession` in memory before the host re-registers — is still exactly what's in `main.py` today (also documented in this repo's `CLAUDE.md`); there's no "wait for host propagation before displaying" branch in the current code, so there are no coded exceptions to it right now.

---

### 2026-07-10 — Confirmed: "pending, locked, awaiting host confirmation" UI exists only on request approve/deny, not on payment toggles

**Context:** User recalled fixing host/admin-screen desync by queuing intent, locking the acted UI element (uneditable, recolored) until the host's confirmation came back, and asked (via git research, no code change) whether this landed on all Render (internet-mode) clients — noting it was never a problem on the LAN/kiosk WiFi-hotspot pages.

**Found via `git log -p -- static/admin.html static/bartender.html`:** commit `a85e852` (2026-06-29, "bartender/admin: color and disable acted request cards immediately") added this to **both** `static/admin.html` and `static/bartender.html` — on Approve/Deny/Play-next click the clicked button relabels to "✓ Sent"/"↑ Sent"/"✗ Sent", siblings fade to `opacity:0.15` + `pointer-events:none`, and an `actedRequests` Map keeps that locked badge showing across every ~5s poll until the request's status stops being `"pending"` (i.e. the host actually confirmed it via `up_next`/`/api/host/sync`).

**Not applied to the payment/settings toggles** (Stripe/Bartender/Auto) in either file — those still flip instantly client-side and just re-fetch true state after a fixed 12s delay (`loadPaymentState`, commit `1977145`), no locked/greyed visual in between. `customer.html` has no equivalent list to lock; its own submit/pay button just does a simpler one-shot "Sending…/Processing…" disable.

**Confirmed absent on the LAN-served variants** (`JukeBar/WebApps/{admin,bartender}.html`, spotonjukebar `assets/{admin,bartender}.html`) — no `actedRequests`/acted-badge code there, consistent with there being no relay round-trip to wait out on those pages.

---

### 2026-07-31 — Full system test plan generated; reconfirmed map registration is transport-agnostic by design

**Context:** User asked for a comprehensive, hierarchical (not tabular — needs to paste cleanly into an
external doc) test case list covering both host platforms, all transports, all modes/features. Saved to
`testing.md` in this repo. Also flagged that Stripe test-key Apple Pay on iOS completes as a success
without an actual charge — noted as a safe repeatable manual test path (test key only).

**Self-correction during this pass, worth recording precisely:** while writing the Community/Discover
Map section, an initial grep of the Android codebase for `listOnMap` was truncated by a `head -10` before
reaching Android's matches (iOS's many hits filled the buffer first, since `~/dev/giffy/JukeBar` was
listed before `~/dev/giffy/spotonjukebar` in the combined grep) — wrongly concluded Android had no
equivalent feature. User corrected this immediately (their own playlist was visibly listed on
jukebars.com from an Android host at the time). Re-investigated properly: Android has it
(`BarDetails.listOnMap`, `ui/setup/NameEntryStep.kt`, `RelayClient.registerMap()`).

**Then a second, more substantive mix-up while investigating the *iOS* side**, also caught and corrected
by the user: initially reported iOS's `registerOnMap()` as gated behind `config.internetMode`. On a
closer re-read, that gate actually belongs to a *different* function, `AppState.swift`'s
`startGenrePoll()` (populates the discover-map genre pie-chart data) — `registerOnMap()` itself
(`AppState.swift:440`) has no transport gate at all, matching Android exactly. This matches the
already-existing decision above ("Map / discover layer is always-on, relay sessions are opt-in" —
`/api/map/register` called regardless of connection mode) — re-confirmed still true in both clients'
current code, not something that needed fixing. `startGenrePoll()`'s gate is legitimate, not a bug: it
depends on a relay `BarSession` existing at all, and `hostRegisterOnRelay()` is correctly
`internetMode`-only (LAN-only bars' admin/bartender are reached via the LAN pages directly, no relay
session exists for them) — so a wifi/hotspot-only bar can appear on `/discover` (name/location/playlist)
but without genre coloring, which is expected, not a gap.

**User's stated principle, worth keeping for future map/connectivity work:** "being on the net should be
a feature for all modes... that can be a runtime determination, not something we code around." Any
future feature that depends on outbound connectivity should default to attempting the call and letting
it fail naturally rather than gating on the configured transport mode, unless (like `hostRegisterOnRelay`)
there's a real structural dependency, not just a plausible-sounding restriction.

## 2026-08-08 — Item 13 (final backlog item): exhaustion-reshuffle no longer scatters pending requests

User's own words closing out the design discussion, after walking through and rejecting two more
elaborate dedup-window alternatives themselves: "why not just freely shuffle the entire playlist, and
play that darn song again, if that is what luck brings." Agreed and shipped the simplest version:
`PlaybackCoordinator.advance()`'s exhaustion-reshuffle now partitions the queue into not-yet-played
requested songs (kept untouched, in place) and filler (freely shuffled) — no deduplication between the
two groups, no windowing, no recursion. Android-only, no relay/iOS involvement. This closes the entire
13-item gap-review backlog opened 2026-08-01.

## 2026-08-08 — Closed two "deliberately out of scope" gaps: LAN player/reports auth, Android admin-PIN hashing

Reviewing the session's accumulated scope-boundary notes, user picked two of four to actually close:
"Go ahead on both 1 and 2" (LAN's unauthenticated `/api/player/*` + `/api/reports*` endpoints, and
Android's plaintext admin-PIN comparison). Left alone: LAN's exposed bartender credential (real refactor,
LAN's physical-presence threat model still holds) and the MDM/kiosk-lockdown ceiling (not fixable from
app code at all, removed from the tracked list entirely rather than carried as a pending item).

Both fixes are Android + iOS only, no relay changes. Player/reports endpoints now require
`isValidAdminToken` on both platforms (previously reachable by anyone on the bar's LAN, no PIN needed).
Android's admin PIN is now SHA-256 hashed everywhere (was the only plaintext holdout — iOS and the relay
already hashed it), with an in-place migration for existing installs' saved plaintext PIN and a small,
deliberate UX change: the wizard's PIN step no longer prefills the old PIN when re-running setup, since
there's nothing meaningful left to prefill with once only a hash is stored.

## 2026-08-09 — Closed item #3: LAN bartender-credential exposure via the Sessions tab

Left as "a real refactor, not worth it" the day before. Revisited after walking through the actual
exploit path concretely (passive LAN sniffing, no detection signal since a stolen token is
indistinguishable from the real bartender using it) — user: "I would not think checking the
bartender tab would be a regular event at all... But if we can implement a prevention of this, then
let's do it!" Turned out much cheaper than the original assessment: added an opaque `session_id`
used only by the Sessions tab's list/kill endpoints, mirroring the relay's existing pattern exactly
— bartenders' actual bearer token (`bartenderId`/`BartenderRecord.id`) is untouched everywhere else.
Both platforms, no relay changes.

## 2026-08-09 — Removed item #4 (MDM/kiosk-lockdown ceiling) from the tracked list

User: "close item 4 too, remove it from the tracked list." Unlike items 1-3, this was never a
deferred-but-doable fix — it's a fact about what's achievable in app code (true unbreakable kiosk
lockdown on either platform requires enterprise MDM device provisioning, a hardware/procurement
decision, not a code change). No code touched; only cleaned up a cross-reference that read as if it
were still an open item. All four items from the 2026-08-08 scope-boundary review are now resolved.

## 2026-08-11 — Added a relay integration-test harness (FakeHost + pytest)

User asked whether the request-flow work could be integration-tested with a "kiosk-simulator,
remote admin, remote customer page." Rather than a full iOS/Android build in CI, built a
lightweight FakeHost (tests/fake_host.py) that speaks the real host_register/host_sync wire
protocol against main.py directly via FastAPI's TestClient — no device, no browser. Two tests cover
the free/auto-accept and pay-to-bartender request lifecycles end-to-end. Deliberately relay-only
scope: doesn't touch Kotlin/Swift, so a PlaybackCoordinator.kt or AppState.swift bug wouldn't be
caught here. Verified the suite's own validity by temporarily breaking a real assertion path
(bartender_requests()'s display-status override) and confirming the test actually failed, not just
trusting green results.
