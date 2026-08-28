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

## 2026-08-17 — Android kiosk physical security: Device Protection replaces failed unpin-detection, gets its own wizard step, and now self-heals

User's real concern turned out to be broader than the original ask ("require PIN to unpin"): "someone
unlocking the pinned device and looking around on the device, including the settings... The app pin is
not a solution here." Built Device Protection instead — `DevicePolicyManager.lockNow()` via a
lightweight Device Administrator grant, triggered from `onStop()` — which force-locks the real OS lock
screen the instant the kiosk app is exited by any means, in any kiosk mode including `remoteOnly`. This
replaced an earlier app-level polling+PIN-recovery mechanism (`KioskUnpinnedScreen`) that misfired live
in testing twice in a row (a false "unpinned" trip right after launch, unresolved even after a warm-up-phase
fix attempt) — rather than chase a third timing bug, removed that layer entirely in favor of the plain
lifecycle-callback approach, which doesn't have that class of bug at all.

User then noticed the new feature was undiscoverable ("I do not recall being asked about agreeing to
this device admin role") — it only ever lived on the live admin screen. Gave it its own non-blocking
wizard step (`WizardStep.DEVICE_PROTECTION`, right after Admin PIN) explaining the gap before asking for
anything.

Final follow-up: once confirmed working, user asked what happens after pinning is exited — nothing
re-established it. User's own proposed fix ("re-press Done from the kiosk admin screen should
re-activate Pinned App") is now wired in, alongside a more general `onResume()`-based re-pin covering
paths that don't go through Admin (e.g. returning through Device Protection's own lock screen). Android
only, no relay or iOS involvement anywhere in this arc.

## 2026-08-17 — Guided Access given its own wizard step, iOS

Follow-up to confirming iOS has no `lockNow()` equivalent at all (checked directly against
`KioskView.swift`, not assumed). User: "ios guided access is EXACTLY what we need... too bad Android
does not have this" — asked for the same wizard-step treatment Android's Device Protection just got.
New `SetupStep.guidedAccess` step in `SetupView.swift`, inserted right after the payment-choices
screen (Stripe/bartender-pay toggles) and before Pricing, explaining the gap and giving the three
manual steps to enable it (Settings → Accessibility → Guided Access; set a passcode/Face ID; triple-click
once the kiosk is running). Deliberately non-blocking — Apple gives no API to check the Settings-level
toggle from inside setup, so there's nothing to gate "Next" on. Build-verified (`xcodebuild ... build`).
No relay or Android changes — Android has nothing equivalent to mirror this into.

## 2026-08-17 — "Both off = free" explanation made always-visible, all three repos (two attempts)

User reported not seeing this explanation on either kiosk-native admin screen. Traced to the text
being purely reactive (only shown once both toggles are already off) on every surface that has it —
kiosk-native (both platforms), `static/admin.html`, and `static/bartender.html` alike. Confirmed via
a quick check that both toggles were in fact off before concluding anything; user then toggled both
off live, saw the text appear, and said: "this should be on all the time!"

First attempt added a *second*, new persistent caption above the toggles, leaving the original
reactive line untouched below. User then reported it missing on the wizard's final/summary screen
— several rounds of diagnosis later, it turned out to be there, just not what was wanted: "I want
the warning that appears BELOW the buttons when I click both buttons to OFF... I want the orange
warning to be there all the time" — plus a wording correction, dash to colon: "Both off: all
requests...". Redone correctly: removed the new caption, made the *original* line unconditional in
its original spot/styling, wording updated on all four surfaces to match.

## 2026-08-17 — iOS setup wizard: Back/Next pinned to the bottom on every step

User: some wizard screens had Back/Next fixed at the bottom with scrollable content above, others
required scrolling all the way down to reach them, "so that if I restart, I do not have to scroll
down to push them all the time." Traced to three steps (name, PIN, pricing) embedding the shared
nav bar inside their own scrollable Form instead of using the already-pinned shared one every other
step used. Removed the three duplicates; all steps but the upload spinner now share one
always-pinned bar. iOS only, not audited on Android.

## 2026-08-17 — Same fix ported to Android's wizard — turned out worse there

User: "check Android's wizard for the same issue." Found it was universal, not partial like iOS —
every one of 9 reachable wizard steps wrapped its entire content and the Back/Next row in one
scrollable Column, so nav always scrolled away, worst on the two steps with potentially long lists
(local folder browser, Spotify playlist picker — one file's own comment admitted "buttons scroll
with the list"). Fixed the same way as iOS: split scrollable content from a pinned nav row on all 9
files. Found and left alone two genuinely dead files with the same pattern (`SetupSummaryStep.kt`,
`FolderPickerDialog.kt` — zero call sites, confirmed via grep) rather than fixing unreachable code.

## 2026-08-17 — iOS bartender PIN field's keyboard couldn't be dismissed

User: entering a new bartender PIN on the live Admin screen left the number-pad keyboard stuck on
screen with no way to close it. Root cause: `.numberPad` has no Return/Done key on iOS, and nothing
else in this field was wired to drop focus. Added `@FocusState` + a keyboard-toolbar Done button
(same pattern already used for the setup wizard's currency field), and made Save also clear focus.
Audited the rest of the Form for the same gap — this was the only affected field.

## 2026-08-17 — Android Spotify Playlist step's nav buttons leaned into the system nav bar

User: buttons on that step sit lower than every other wizard step, into the nav bar area. Every
other step's nav row already had `.windowInsetsPadding(WindowInsets.navigationBars)`; this one file
never did — a pre-existing gap carried forward, not introduced, by the earlier same-day pinned-nav
fix. Added the missing inset to match every sibling step.

## 2026-08-17 — Bartender Access PIN control simplified, both kiosk-native Admin screens

User: "way too much content for this one setting" — proposed a single field that shows masked
dots when a PIN is set (tap to clear and re-enter), with Save appearing only when there's an actual
pending change, including clearing to empty meaning "disable." Implemented on both iOS and Android,
folding the old separate always-visible status text + field + Save + Turn Off button into one
control. Footer collapsed to the user's own suggested one-liner. `static/admin.html`/LAN admin.html
have the identical overloaded pattern but were deliberately left alone — scoped to "both platforms"
meaning native apps, not a sweep of every surface.

## 2026-08-17 — Android "List on JukeBar map" switch style unified with Payments switches

User noticed the styling mismatch and asked why; no documented reason found, just an unreconciled
one-off (black thumb + solid track vs. every Payments switch's Coral thumb + translucent track).
User: "Yes, go ahead and fix it." Unified. While checking for the same issue on iOS, found a
similar-shaped but not identical mismatch (Payments toggles have an explicit `.tint(.orange)`,
"List on JukeBar map" has none) — flagged to the user, left unfixed since the ask was Android-only.

## 2026-08-17 — Admin screen header split into two rows so long bar names can't be truncated

User: "do not truncate to force onto a single row" — both platforms crammed logo/JukeBar/bar
name/Admin/Done into one line (Android: plain Row, could overflow; iOS: toolbar .principal item
with lineLimit(1), actually truncated with an ellipsis). Split into two rows on both: row 1 is
logo + JukeBar + bar name (wraps), row 2 is "Admin" + Done. iOS needed a bigger change since a
toolbar principal item can't grow taller — pulled the header out of the toolbar into a plain
VStack above the Form, hiding the nav bar entirely; Done became a plain button in that header.

## 2026-08-17 — Two small iOS polish fixes: Admin header logo size, Guided Access caption wording

User asked for both together. (1) Admin header logo (22pt, added in the two-row header fix above)
enlarged to match the customer/kiosk page's capped logo size (80/104pt) — logo only, text sizes
left as-is since that's what was asked. (2) Kiosk's Guided Access warning caption shortened from a
full sentence to "Enable Guided Access!" — user: "the rest is not on the screen." The fuller
explanation still lives in the wizard's dedicated Guided Access step; this caption was always meant
as a brief nudge, not the primary explanation.

## 2026-08-17 — Render bartender.html now asks for a name (closes a real multi-bartender gap)

User confirmed a round of live testing (render admin/bartender, iOS host): PIN set/unset showing/
hiding the QR, logging in as bartender, Kill + bundled PIN-change all worked. While answering their
question about whether name-taking still happens on LAN, found render's `bartender.html` never
collected one at all despite the relay backend already supporting it — every internet bartender
showed up as the literal "Bartender" on the Sessions tab. User: "without taking names we will have
a clusterfuck.. How would I know which session to kill if there is a hacker bartender?" Added the
same "Your name" field LAN already has; no backend change needed, the field was already accepted
and stored, just never sent from render's own page.

## 2026-08-17 — Correction: bartender QR is kiosk-native only, not on render/LAN admin.html

I'd claimed QR codes show on 3 places (kiosk-native + LAN admin.html + render admin.html). User
pushed back, correctly: "QR codes for bartender logins ONLY are displayed on either iOS kiosk admin
page or android kiosk admin page." Checked directly (grep for QR-generation code) rather than
re-asserting — user was right. Render and LAN admin.html only show status text, no image, no
visible link at all; only kiosk-native (`QRImageView`/`generateQrBitmap`) actually renders one.
Fixed a wrong "QR appeared on render" annotation I'd just added to the test plan in the process.

## 2026-08-17 — Bartender QR added to render/LAN admin.html Sessions tab; sort order fixed

User: "let remote admin surfaces carry the bartender QR code... Do it!" Implemented on all three
(relay via new Python `qrcode` dependency, iOS/Android LAN via each platform's already-proven QR
generator, reused not reimplemented) — round-trip-verified the relay's output before shipping
(encoded, decoded with OpenCV, confirmed match). User redirected placement mid-build: "the QR codes
should go under the Sessions tab above the already connected sessions" — moved from the
Bartender-Access-PIN card to a new card atop the Sessions pane on all three.

Separately, same message: "sessions should be listed in chronological order of sign in, earliest
first, latest last (possible duplicates, hackers...)" — relay was sorting latest-first, flipped.
Android was already correct (in-memory append-ordered list, no sort needed). Found a real bug on
iOS while checking: its list came from a filesystem directory listing with no guaranteed order at
all — added an explicit sort. Been silently unordered since the Sessions tab shipped 2026-08-02,
never caught before.

## 2026-08-17 — Bartender's own name now shown on their own screen; "Updated" moved off the header

User's live-testing round on render (names propagate to admin, multiple bartenders login, Kill
works) surfaced two gaps: "the bartender's name is NOT displayed on their own screen... right after
[bar name] - [bartender name]; use diff color... the text Updated [datetime]... should go onto the
scrollable area top... make sure you do these changes also to the lan-based bartender/admin pages."
Fixed on all three: render's previously-hidden `.header-role` div now shows the bartender's own
name in the existing peach accent color, and its "Updated" badge moved into the scrollable
requests pane. LAN's "Updated" was already correctly placed (confirmed by reading the code, not
assumed) — only the name-display gap needed fixing there, via a new `name` field on both LAN
platforms' pair/status responses.

## 2026-08-18 — iOS: fixed a real "no auto-restore" bug that silently rotated sessions

User: bartender session on render said "no longer valid" despite not closing the kiosk app — asked
me to confirm sessions only expire via explicit new-session or Kill. Traced to a genuine gap:
`AppState.swift` never restored `isSetupComplete` on a cold launch (its own comment said "no
auto-restore"), so any process kill — not just an intentional End Session — landed back on the
wizard, and completing it (even via pre-filled fields) minted a new session, silently invalidating
every bartender token. Android already did this correctly (confirmed by reading
`MainActivity.restoreSetupState()`). Fixed with the same shape of restore, plus a dedicated
persisted "setup complete" flag (not just inferring from file presence, since End Session
deliberately leaves those files on disk) and a migration for already-live installs upgrading to
this fix.

## 2026-08-18 — Reverted the above same day: user's explicit design call, plus a corrected diagnosis

Two things overturned it: the original report's host was actually Android, not iOS ("the host must
have been on Android"), so the iOS fix was never the real explanation; and an explicit reversal of
the whole premise: "if the session restarts... the session RIGHTFULLY should end... so kiosk
session restart should wipe the sessions and force a new session." Reverted `AppState.swift` in
full back to "no auto-restore." Actual likely cause of the original report: Render auto-deploys on
every push to jukebarweb's `main`, and this session pushed to `main.py` repeatedly while the user
was live-testing — each deploy restarts the relay process and wipes `_bars` (in-memory only),
killing every bartender token with zero host involvement. Accepted as expected/by-design, not
fixed — matches the user's own stated position. Lesson: avoid pushing to jukebarweb's `main` while
the user is actively live-testing a session.

## 2026-08-18 — Bartender names must be unique among active sessions, all three pairing backends

User tested pairing two bartenders as "Ted" on Android LAN — allowed, shouldn't have been ("we
talked about bartender names under a bar's sessions should be unique"). Asked me to check all
platforms including LAN and "kiosk back end." Fixed in all three independent implementations
(relay `main.py`, Android `LocalRequestManager`, iOS `LocalServer.swift`) — case-insensitive
uniqueness check against currently-active sessions only (confirmed Kill genuinely removes records
on each platform, not just marks them, so a freed name is immediately reusable), 409 on conflict.
Android/iOS bartender.html pages needed no JS changes (already had a generic error fallback);
render's did need one explicit branch added.

## 2026-08-18 — Bartender name now required (min 2 chars), all three backends + pages

Same testing round as the uniqueness fix above: render's bartender login "allowed me to log in
without giving a name" — every backend still silently defaulted blank to "Bartender," which is
exactly the ambiguity the whole feature exists to prevent. User: "length should be 2 or greater;
not 4" (name minimum is separate from the PIN's 4-digit one) and "all surfaces and platforms."
Fixed in all three backends (relay, Android LAN, iOS LAN) with the same PIN → name-length →
name-uniqueness ordering, plus client-side gating added to all three `bartender.html` pages — the
two LAN pages' Pair buttons previously had no gating at all (always clickable).

## 2026-08-18 — LAN admin.html's standalone "System" tab merged into "Sessions"

User: LAN admin has "no Session tab but have System tab instead (that render does not)" and the
bartender QR "is not present... despite your earlier claims." Checked directly — the QR and
Sessions tab were both genuinely already there (added earlier this session); the real bug was LAN
having 5 tabs (with an unfamiliar System tab right before Sessions) vs. render's 4, so live testing
landed on System first and concluded nothing was there. Followed the user's exact fix direction:
removed System as a separate tab on both platforms, moved its content (`#system-info`) to the
bottom of the Sessions panel, below the QR/session-list/lockout stack that was already correctly
ordered. Both LAN pages now have 4 tabs, matching render.

## 2026-08-18 — LAN bartender.html: misleading "ended by admin" message before ever pairing

User: entered correct PIN but a short name on LAN bartender login, got "Your access was ended by
the admin — enter the PIN again to continue" — a misleading message. Traced past the obvious
suspect first: the short-name client-side guard was already correctly blocking submission, so the
message couldn't have come from a rejected pairing attempt at all. Real cause: both platforms'
`setInterval(loadRequests, 15000)` runs unconditionally from page load, unlike its sibling
`loadPaymentState`'s interval which only starts after pairing succeeds — an unpaired page has an
empty `bartenderId`, so the interval's own background call sent an empty token, got a genuine 401,
and fired the "kicked" message on a session that never existed. Fixed with a one-line guard
(`if (!bartenderId) return;`) at the top of `loadRequests()` on both `assets/bartender.html`
(Android) and `WebApps/bartender.html` (iOS). Render's equivalent interval was already correctly
scoped inside `startMain()` — confirmed, no fix needed there.

## 2026-08-18 — Turning off bartender access didn't kill existing sessions, all three backends

User on render: turned off bartender access with a bartender session live — the tab eventually
showed a raw `{"detail":"Not found"}` JSON blob instead of a friendly message, and the killed
session kept listing as "active" on the Sessions tab. Root cause: clearing the PIN never touched
`bartender_tokens`/local bartender-session stores at all — only *token presence* was ever checked,
never whether a PIN is currently set — so an existing token authenticated forever, and the only way
to see any "kicked" state was a hard page reload hitting the pre-existing hard-lockout 404 gate
(built for "never had bartender access," not "access was just revoked," so it had no friendly
response). Fixed with two changes on all three backends: (1) immediately purge every active
bartender-role session the moment the PIN goes empty (relay's `host_sync()` echo, Android's
`purgeAllBartenderSessions()`, iOS's `deleteAllBartenders()` — no host round-trip needed, same
shape as the existing Kill action), and (2) the PIN-off 404 now returns a small styled "Bartender
access unavailable" HTML page instead of bare JSON/empty body, on all three page routes. Tab-close
detection (also asked about) is confirmed not solvable server-side and not attempted — matches this
codebase's existing "no automatic expiration" design for bartender sessions.

## 2026-08-18 — LAN admin.html's Actions/Sessions tabs weren't live-refreshed, Android + iOS

User enabled bartender access from the kiosk-native Admin screen while on LAN — the already-open
LAN admin.html kept showing it off, no QR on Sessions. Traced the data path before assuming
anything: `barDetails` propagation to the LAN `LocalServer` was correct and synchronous, and
`/api/catalog` read it live on every call — not a propagation bug. The real gap: `loadActions()`
(drives both the Actions toggle display and the Sessions tab's QR visibility) was only ever called
on page load or an explicit Actions-tab click, never on `switchTab('sessions')` and never on a
timer — unlike Requests/NowPlaying, which already have their own poll loops. Fixed on both
platforms by extending the existing gated 5s poll timer to also call `loadActions()` whenever
either the Actions or Sessions tab is active. Render's `static/admin.html` already polls
unconditionally every 5s and already refreshes this state — confirmed via code read, no fix
needed there.

## 2026-08-18 — Android startup race could drop a kiosk-set bartender PIN, Android only

User set the bartender PIN from kiosk-native Admin right after starting the app — kiosk's own
screen correctly showed the QR, but LAN admin kept saying access was off, and scanning the kiosk's
own QR hit the new "Bartender access unavailable" page. Traced to `startLocalServer(details)`
baking a one-time `details` snapshot into the new `LocalServer` before finally reassigning
`localServer = server` — a settings change made via the Admin screen (reachable the instant
`isLaunchKiosk` flips true, independent of whether `startQueue()`'s coroutine has reached
`startLocalServer()` yet) could land on the not-yet-reassigned `localServer` reference and get lost
when the new object then pointed at its already-stale baked-in snapshot. Fixed by re-syncing from
the live `barDetails` immediately after `localServer = server`. iOS confirmed to have no equivalent
bug — its LAN routes read config fresh from disk on every request, no in-memory snapshot to go
stale. A related claim (a LAN admin tab left open across a restart "kept working") wasn't
independently confirmed as a distinct bug — flagged back to the user to retest since this race may
have been the actual cause.

## 2026-08-18 — Follow-up: the real bug wasn't a race, it was a missing propagate call

User's precise counter-repro (deliberately unhurried, waited) ruled out timing entirely. Real cause:
`AdminScreen` is wired from two places — the live post-launch overlay's callbacks correctly call
`propagateBarDetails()`, but the wizard Summary screen's `onBarDetailsSaved` handler never did,
so any toggle made there (PIN, Stripe, Bartender, AcceptingRequests) updated `barDetails` (kiosk's
own QR read it fine) but never reached the running `LocalServer`/`RelayService` at all — a
permanently missing wire, not a window. Fixed by adding the missing call; safe unconditionally
even pre-launch. iOS has no equivalent split (`AdminView` always reads/writes the same
`AppState`/`LocalStorage` singleton, wizard or live).

## 2026-08-18 — Bartender Sessions list still needed manual refresh, all three admin surfaces

After confirming the missing-propagate fix worked, user noticed the Sessions *list* itself
(paired bartenders + lockouts) still needed the manual Refresh button or a tab round-trip, unlike
everything else on that tab. The earlier live-refresh fix only wired up `loadActions()` (QR card +
Actions toggles), never `loadBartenderSessions()`/`loadSystem()`. User asked me to check render
too rather than assume LAN-only — correct guess, same gap existed there (`poll()` never called
`loadBartenderSessions()`). Fixed on all three `admin.html` files: LAN pages' poll timers and
render's `poll()` now also refresh the session list while the Sessions tab is active.

## 2026-08-18 — LAN bartender.html: stale localStorage credential poisoned a fresh pairing attempt

A second bartender opening the LAN page on a device that had paired a different bartender before
saw "ended by the admin" without ever submitting anything. `bartenderId` seeds from `localStorage`
(shared across any bartender who ever paired on this device) — `checkStatus()`'s failure branch
fell back to the pair screen correctly but never cleared it, so the still-unconditionally-armed
15s `loadRequests()` interval picked up the stale id moments later, got a real 401, and showed the
alarming message. Fixed by clearing `localStorage`/`bartenderId` in that failure branch too, both
platforms. Render unaffected — its `sessionStorage` is scoped per-tab by design.

## 2026-08-18 — LAN admin.html had no session-expiry detection at all, both platforms

User asked directly whether LAN admin sessions survive a restart. Server-side: no —
`LocalRequestManager.reset()` clears admin tokens on every wizard-completion cycle, which always
re-runs on restart. Client-side: `admin.html` had zero 401 handling anywhere (unlike
`bartender.html`'s `bartenderKicked()`), so a dead token just made every admin action silently
fail with no visible message — likely the real explanation for the earlier "leftover tab kept
working" observation (public endpoints kept updating, masking that authenticated actions were
actually broken). Added a shared `adminKicked()` wired into the highest-traffic admin-token-gated
calls (main poll, Sessions list, the three payment toggles, bartender PIN save/clear) on both LAN
admin.html files. Render already handles this correctly via its existing "Session expired" path —
not touched.

## 2026-08-18 — LAN admin token survived a real app restart; closed via PIN-reset purge

User's controlled test (full swipe-off restart, walked through wizard, then toggled a setting on
an already-open, never-reloaded LAN admin tab) confirmed the pre-restart admin token still worked
— contradicting the expectation that a fresh `LocalRequestManager` starts empty. Root cause not
conclusively pinned down (would need live Logcat/PID inspection); user reframed the actual
requirement correctly: a stale session surviving on the same trusted device is minor, but there
needs to be a *reliable* way to kill a genuinely bad admin session, independent of restart
mechanics. Found that neither platform's admin PIN reset ("Forgot PIN" flow) ever invalidated
already-issued tokens — defeating its whole purpose as a security-recovery flow. Added
`purgeAdminTokens()` on both platforms, called right after the PIN is saved.

## 2026-08-18 — Likely real root cause: missing onTaskRemoved(), Android only

User correctly rejected the PIN-reset mitigation as not actually explaining or fixing the restart
behavior, then independently described the standard correct architecture (session restart should
mint a fresh id, everything reconnects, closing the session should break all connections) —
prompting a proper re-investigation. Found `LanForegroundService` (exists purely to stop OEM
battery managers from freezing the LAN server, holds no reference to the real server) never
overrode `onTaskRemoved()` — the Android hook specifically for "task was swiped away," since a
foreground service can otherwise outlive that. Combined with `START_STICKY` (no reason to
self-resurrect), this could let the *previous* NanoHTTPD listener — with its own never-reset
admin tokens — keep answering requests independently of whatever a later launch starts. Fixed:
`onTaskRemoved()` now tears down the real server via a registered callback and stops the service
immediately; switched to `START_NOT_STICKY`; wrapped the previously-uncaught server bind call in
try/catch as a second safety net. Not yet independently confirmed against a live repro.

**Update, same day**: user confirmed the fix — stale tab kept showing now-playing but got kicked
to the PIN screen the moment a real action was tried. Asked if iOS has similar protections; found
a worse, unconditional gap: iOS's `LocalServer.shared` is a true process-lifetime singleton whose
`adminTokens` was never cleared anywhere, including `resetSetup()` (End Session) — an admin token
stayed valid across any number of new sessions for as long as the app process lived. Fixed by
calling `purgeAdminTokens()` from `resetSetup()`.

## 2026-08-28 — Auto-manage requests: Manual/Auto mode for accepting_requests, all 13 surfaces

Implemented the "Managing requests" feature recorded as a design-only placeholder in CLAUDE.md on
2026-08-22. User: manual mode keeps today's Start/Stop button; auto mode uses two watermark
numbers (default 10 stop / 5 resume) to auto-manage `accepting_requests`, mode switches take
effect immediately, and both must live in the same UI area as the existing toggle rather than a
new section. Resolved the design doc's open question (outstanding = pending + up-next, not just
pending-awaiting-approval) in favor of the broader definition. Relay: three new self-healing
`BarSession` fields (`auto_manage_requests`/`auto_manage_max`/`auto_manage_restart`), riding the
exact same desired_settings/echo mechanism as the three existing toggles, admin-token-gated.
`static/admin.html`'s Requests card got the Manual/Auto switch + threshold fields; smoke-tested the
full register→settings→sync-echo→pending-clear cycle directly against a local server before
trusting it. iOS and Android implemented in parallel (background forks) — both independently
arrived at the identical wire contract, confirmed by diff review; each added a shared
`setAcceptingRequests()`/equivalent setter used by both the manual toggle and the new host-side
watermark evaluator (transport-independent: iOS's existing 2s ticker, Android's new 5s loop
started in `startQueue()`), a new wizard step, and matching kiosk-native + LAN admin.html UI with
the manual toggle disabled while Auto is active. Verified all three repos build clean myself
(fresh `xcodebuild`/`gradlew compileDebugKotlin`) rather than trusting fork self-reports alone,
after one fork's report described briefly touching the other platform's repo when it hit a
same-agent-can't-nest-fork limitation — working-tree diffs on both host repos came out as single,
coherent, non-duplicated changesets, so no corruption resulted.

## 2026-08-28 — Auto-manage copy rewrite + "outstanding" narrowed to approved-only

User gave exact copy for the auto-manage section ("Choose how you want to stop accepting more
requests:" / "Manually" / "Automatically" / "Start/Stop on the Admin screens." / "Automatically
stop taking requests when reaching set number of outstanding approved requests, and resume when
this number falls below set number.") — applied verbatim across all 13 surfaces (render
admin.html, both LAN admin.html files, both kiosk-native Admin screens, both wizard steps).
Reviewing that exact wording ("outstanding approved requests") against the shipped counting logic
(which still included `pending`) surfaced a real gap: a bad actor could spam pay-to-bartender
requests with no intent to pay, inflating the outstanding count to trip auto-stop and freeze
requests for real customers. User asked whether per-IP request throttling could help, and whether
customers get distinct IPs over hotspot/wifi (yes — the host's own DHCP/router assigns each device
a distinct LAN-local IP in both modes, same assumption the existing per-IP PIN-lockout mechanism
already relies on) — but then proposed a simpler fix instead of IP tracking: exclude `pending`
requests from the count entirely, since an unreviewed request can't do anything until a bartender
approves it (which is also the payment-confirmation moment for pay-to-bartender), and a suspicious
burst of pending requests is already visible to the bartender to deny directly. Implemented across
all three repos — only `approved`/`approved_jump` count toward the watermark now. Closes the abuse
angle architecturally, no new anti-abuse mechanism needed. All three repos rebuilt clean and
re-pushed.

## 2026-08-28 (later same day) — Per-requester outstanding-request throttle, complementary to auto-manage

User's follow-up scenario: even with auto-manage narrowed to approved-only, one anonymous guest
could still spam free/pay-to-bartender requests to clutter the admin/bartender Requests screen
(no cost, no auto-stop trigger since it's all pending). Asked whether per-IP throttling works over
WiFi/Hotspot — confirmed yes (each device gets its own DHCP-assigned local IP, same assumption the
bartender-PIN lockout already relies on), but flagged internet-mode customers can share a public
IP behind CGNAT/café NAT. User then proposed combining IP with the existing customer_id
(browser-persisted localStorage id already sent with every request) rather than IP alone.
Implemented as a union of both signals (not their intersection): a request is rejected if the
requester's own outstanding (pending+approved, not yet played/denied) count already exceeds 2,
matching on either the same source IP or the same customer_id — so an abuser has to evade both to
reset their count. Relay, iOS LocalServer, and Android LocalServer all check this at request-
creation time (bar_request()/handleSubmitRequest equivalents), not at kiosk-native or Stripe
payment endpoints. Smoke-tested directly: 3 requests from one customer_id succeed, the 4th 429s;
a different customer_id sharing the same source IP also correctly collides, confirming the
union-match behaves as designed. Found and fixed a latent Android bug along the way: jsonError()
had no 429 case (would have returned 500). All three customer.html copies show a lightweight
alert() on 429, not the heavier full-screen error state used for expired/offline sessions.

## 2026-08-28 (later still) — Per-requester throttle refined: pending songs only, approved excluded

User walked through a concrete scenario: X requests 3 songs, approved and queued to play — X
should still be able to request 3 more (0 songs actually awaiting review); only after that second
batch (bringing pending to 3) should X be barred, until it drops below 3. "Already approved (and
paid) requests do not count here" — explicit. Reworked to count individual songs (not request
objects) in status=="pending" AND payment_method != "stripe" requests only. Renamed
MAX_OUTSTANDING_REQUESTS_PER_REQUESTER -> MAX_PENDING_SONGS_PER_REQUESTER,
_requester_outstanding_count() -> _requester_pending_song_count(). First manual retest looked
broken (2nd submission still blocked after approving the 1st) - traced to bartender_approve()'s
own documented behavior ("host confirms via up_next on next sync", raw status stays "pending"
until then) - not a bug, the architecture working as designed. Retested with a simulated
/api/host/sync call echoing the confirmed status in between submissions - behaved exactly as
specified. iOS/Android LAN approve handlers flip status synchronously (no host round-trip needed
there), so this timing note is relay-only.

## 2026-08-28 (later still) — Auto-manage number fields: select-all-on-focus + visible styling

User: hard to see the auto-manage number fields are inputs, hard to select the existing number to
overwrite it. Root cause on the HTML surfaces: `.am-num-field`'s background was essentially the
same shade as its own container card, border nearly invisible. Fixed with a lighter fill + more
visible border on all three admin.html copies, plus `onfocus="this.select()"` so a tap highlights
the existing digits for immediate overwrite.

## 2026-08-28 (later still) — Mode selector redesigned as an either/or knob, all 7 places

User: the Manually/Automatically button pair "is more like seeing options" than an either/or
setting; wanted "a knob that they either turn toward Manually or toward Automatically," and only
the controls for the currently active mode should display at all - not the other mode's controls
faded/disabled underneath. Replaced the two-button row with the existing single-switch component
(`.toggle-track` on render, `.lan-toggle` on both LAN pages - reused, not reinvented) whose label
text flips between "Manually"/"Automatically". Below it, only the active mode's block renders in
the DOM at all. Same redesign applied to iOS (Toggle) and Android (Switch) kiosk-native + wizard
surfaces in the same pass. Dead .mode-switch/.mode-btn CSS removed from all three admin.html
copies.

## 2026-08-28 (later still) — Knob redesigned again: slide control replaces on/off switch

User caught a second, distinct issue with the previous fix: a boolean switch still implies
"off = disabled" regardless of what label sits next to it, which doesn't fit two equally-valid
named positions. Proposed: show both labels above a wide, fat slide control where thumb position
alone (not color-as-enabled) conveys the setting. Replaced the toggle-track/lan-toggle switch with
a wider track whose thumb fills exactly half and sits on whichever side is active. Applied to all
7 places: 3 admin.html copies (new .am-knob-track/.am-knob-thumb/.am-knob-labels CSS), iOS
(new ModeKnob.swift, HStack+Spacer trick), Android (new ui/ModeKnob.kt, fillMaxWidth(0.5f) +
alignment).

## 2026-08-28 (later still) — iOS ModeKnob bug: thumb was always full-width solid orange

User caught live on-device: the knob was solid orange with no visible position/movement. Root
cause: a bare Shape (RoundedRectangle) has no intrinsic size and expands to fill all available
space in a stack, same as Color - the HStack+Spacer trick used to try to get "half width" didn't
actually constrain it, it just filled the entire track regardless of state. Fixed with an explicit
GeometryReader-computed width/offset. Android and the HTML surfaces were never affected (Compose's
fillMaxWidth(fraction) and plain CSS width are unambiguous).

## 2026-08-28 (later still) — All settings controls made optimistic; mode knob got "...updating"

User: the mode knob had no "...updating" indicator at all, leading to repeated re-clicks while
waiting on the relay's next host-sync cycle. Investigating surfaced a broader gap: every settings
control froze showing the OLD value while pending instead of the desired one - S.stripeEnabled
etc. were never touched until poll() received the host's real echo. Fixed by setting the
optimistic value immediately in togglePayment()/setAutoManageMode()/saveAutoManage(), before
marking the field pending - poll()'s unconditional resync corrects it either way, so a success is
invisible and a failure reverts via a saved previous value. Added #am-knob-updating for the mode
knob's missing indicator. Scoped to render only - checked both LAN admin.html copies first and
confirmed the same multi-second-wait problem doesn't apply there (synchronous apply, no host-sync
cycle to wait through).
