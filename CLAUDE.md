# JukeBar — Relay (jukebarweb)

## Spec & docs
All product specs live in `docs/` in this repo:
- `docs/ios_spec.md` — full iOS product behaviour, API, session lifecycle, visual identity
- `docs/android_spec.md` — Android-specific differences (Spotify instead of Apple Music)
- `docs/render_spec.md` — planned discovery/genre-profiling pipeline (Last.fm + recommended playlist)
- `docs/architecture.html` — system architecture reference (entry points, API/route inventories,
  data flows, deployment topology) across all three repos + Home Mac Server. Hand-maintained, not
  generated — served at the unlisted `jukebars.com/youwouldnotguesss/architecture` route in main.py,
  not linked from any public page. **Keep it in sync**: when a task changes a file that's one of its
  16 diagram nodes (see the node list / info-panel content in the file itself), or adds/removes an
  API route, or changes what talks to what, update the diagram, the affected table row, and that
  node's info-panel entry in the same change — don't let it silently drift.

Architecture decisions belong in this CLAUDE.md, not in the spec files.

---

## What this service does
Render-hosted relay so bars can operate over the internet (not just LAN).
- Host registers at startup, syncs every 5 s
- Customer and bartender browsers poll this service
- Admin browser posts setting changes here; they queue as actions for the host

## Architecture: host is source of truth
The Android/iOS host app owns all state. The relay is a message-passing layer, never an independent
author of truth:
1. Host registers with full catalog + bar details
2. Admin/customer/bartender pages read from relay
3. Setting changes from admin → relay queues action → host picks up on next sync → host re-registers with new state

**Governing principle (2026-07-20): every surface writes to host app state; host app state
broadcasts itself out to every client.** A client action never updates relay state directly and
has that stand as truth — it must change the host's own state (directly if host-local, or via a
relay-queued action the host applies on its next sync), and the host's next sync unconditionally
re-asserts its full current state to the relay. Applies uniformly to settings, `now_playing`,
`up_next`, and song requests, regardless of origin (kiosk, LAN web, internet web, bartender,
admin). Two channels ride the same 5s heartbeat: **upstream** (`new_requests`, `actions`,
`desired_settings` — delivered in the sync *response*, since the relay can't push to a polling
host) and **downstream** (`settings` echo, `now_playing`, `up_next`, `requests` — broadcast
unconditionally every tick). Stripe fits without special-casing: a confirmed payment just joins
`new_requests` upstream like any other newly-known request.

**`bar.requests` full compliance:** `host_sync()`'s `requests` field carries the host's complete
local request list (any status) every call; the relay **upserts** each into `bar.requests` — never
deletes (a relay-born request may not be in the host's echo yet; host-aged-out history shouldn't
vanish). For an id the relay already knows (born relay-side), only `status`/`jump`/`approved_at`/
`paid` are trusted from the host — `price`/`payment_method`/`requester_name`/`song_ids` stay
whatever the relay originally recorded. Kiosk (iOS/Android) always just saves to host-local state
regardless of network/approval mode — one path, no relay-awareness at request-creation time.
`LocalStorage`/`LocalRequestManager` are the single request store per platform for every request
origin (see `DECISIONS.md`'s archive for the retired `played_request_ids`/`pendingRequests`
mechanisms this replaced). One piece of host-local bookkeeping stays separate by design:
`injectedRequestIds`/queue-position tracking (which currently-playing song belongs to which
request) — a live-queue-position question a flat request list doesn't answer, not request state.

**Settings propagation, single-slot design:** an admin/bartender toggle POSTs to
`/api/bar/{id}/settings`, which writes the desired value into `BarSession.desired_settings[field]`
— a single latest-write-wins slot per field, not a queue — and does **not** touch the live
`bar.*` field directly. The host picks up `desired_settings` on every `/api/host/sync`, applies it
locally, and its own current values — sent unconditionally on every sync regardless of who changed
them — are what actually update the live field on the relay (no version/ordering guard needed; the
host just re-asserts truth every 5s, so a dropped/out-of-order call self-heals next tick). A
`desired_settings` entry clears itself once the host's echo matches it. Client toggles stay locked
(dimmed) exactly as long as their field name appears in `settings_pending`
(`= list(bar.desired_settings.keys())`).

**Register is never used for a routine toggle** — only for startup, a genuinely new session, or a
catalog refresh. `host_register()` updates an existing `BarSession` in place (same `jukebar_id` +
`session`) rather than replacing it, so a settings change never risks wiping `now_playing`/
`up_next_queue`/pending requests — settings and playback-display state flow through completely
separate fields with no shared replace-the-whole-object step.

Covers today: `stripe_enabled`, `bartender_enabled`, `accepting_requests`, `bartender_pin_hash`,
`auto_manage_requests`/`auto_manage_max`/`auto_manage_restart`. **Any new admin/bartender toggle
needs**: `bar_settings()`'s field tuple, `host_sync()`'s echo/clear logic, the
`/api/bar/{id}/requests` response, both relay HTML files' Actions tab, iOS `AppState.swift`'s
`settingsEcho`/`applyDesiredSettings()` + `LocalServer.swift` (`/api/admin/settings`, LAN-only,
direct apply) + both LAN HTML files, and Android `RelayClient.kt`/`RelayService.kt` equivalents +
`LocalServer.kt` (`handleAdminSettings`) + both LAN HTML files. Missing one of these silently
breaks only that surface — exactly how `accepting_requests` went unnoticed for ten days before
this design existed. Full narrative history (the version-numbers design this replaced, and every
dated fix/feature built on top of it through 2026-08-28) is archived verbatim in `DECISIONS.md`
under "Archived from CLAUDE.md (2026-09-30)" — read it before touching any of this mechanism if
the summary above doesn't answer the question.

## UI surface matrix (13 surfaces, all must stay in sync)
JukeBar has three codebases — iOS (`~/dev/giffy/JukeBar`), Android (`~/dev/giffy/spotonjukebar`), and this relay (jukebarweb). Every page type exists across multiple delivery surfaces and (except Bartender) both host platforms. Any task touching admin/bartender/customer UI or behavior should be checked against every relevant cell below, not just the one surface mentioned. Look and feel should be near-identical (ideally identical) across all surfaces of a given page type. The Render (internet) cell is a single shared page regardless of which platform is hosting — there is no iOS/Android branching in `static/*.html` or `main.py`.

### Admin (5 surfaces)
| Surface | iOS | Android |
|---|---|---|
| Kiosk-native | `JukeBar/AdminView.swift` | `ui/AdminScreen.kt` |
| WiFi-served LAN HTML | `JukeBar/WebApps/admin.html` (served by `LocalServer.swift`) | `assets/admin.html` (served by `local/LocalServer.kt`) |
| Internet (Render), shared | `static/admin.html` — this repo | same |

### Bartender (3 surfaces — no kiosk-native; bartenders use their own device, not the host kiosk)
| Surface | iOS | Android |
|---|---|---|
| WiFi-served LAN HTML | `JukeBar/WebApps/bartender.html` | `assets/bartender.html` |
| Internet (Render), shared | `static/bartender.html` — this repo | same |

### Customer request (5 surfaces)
| Surface | iOS | Android |
|---|---|---|
| Kiosk-native | `JukeBar/KioskView.swift` | `ui/KioskView.kt` + `ui/LocalRequestSheet.kt` |
| WiFi-served LAN HTML | `JukeBar/WebApps/customer.html` | `assets/customer.html` |
| Internet (Render), shared | `static/customer.html` — this repo | same |

### Kiosk-native customer surface — confirmed cross-cutting invariants (verified 2026-07-17)

Kiosk-native (`JukeBar/JukeBar/KioskView.swift`, `spotonjukebar/.../ui/KioskView.kt` +
`ui/LocalRequestSheet.kt`) is a separate implementation from `customer.html` (LAN and Internet), not a
shared codepath — see the matrix above. Three behaviors that are easy to assume are "web-only" are
**already implemented on kiosk-native too, on both platforms**, confirmed by direct source read:

1. **`accepting_requests` gates local request submission, not just the web pages.** iOS:
   `allowsLocalRequest` in `KioskView.swift` checks `cfg?.acceptingRequests`. Android:
   `showLocalRequestButton` in `KioskView.kt` checks `barDetails?.acceptingRequests`. When off, the
   kiosk still shows now-playing/QR (people can still see what's playing) but hides/disables the
   Request button — it does not silently keep accepting local requests.
2. **`requesterName` is the field name kiosk-native uses too**, not a legacy `customerName`. iOS:
   `CatalogBrowseView.swift`'s local-request sheet (`requesterName` state var, passed through to both
   the relay post and the local `SongRequest`). Android: `LocalRequestSheet.kt` (`requesterName` state
   var → `onSubmit(requesterName, ...)`).
3. **Up Next has kiosk-native equivalents of the web badge behavior**, not the same HTML/CSS but the
   same intent: a small always-visible preview strip (row count driven by screen height, iOS
   `upNextRowCount()` / Android `upNextRowCount()`), plus a tap-to-expand full-queue overlay that
   **self-dismisses after 15s of no interaction** (iOS `ApprovedQueueOverlay.resetIdleTimer()`, Android
   `LaunchedEffect(...) { delay(15_000) }` in the queue overlay) — both re-verified identical between
   platforms.

**Why this note exists:** these three were flagged as unverified "does kiosk need the same treatment?"
open questions in a testing checklist before being checked against source — they were already correct,
but nothing had documented that a kiosk parity check should look here first before assuming a gap. Save
the re-investigation next time: check these three call sites before treating kiosk-native as behind.

## Key decisions

This section is a condensed index of standing decisions/current behavior. **Full narrative detail
for every entry below — what was tried first, exact bugs, user quotes, why alternatives were
rejected — is archived verbatim in `DECISIONS.md` under "Archived from CLAUDE.md (2026-09-30)".**
Read the archive before making a related change if a one-liner here isn't enough context; don't
assume the short form captures every edge case the original entry reasoned through.

**Transport / kiosk mode / Stripe interaction:**
- Transport (`wifi`/`hotspot`/`internet`) is always chosen independently of kiosk display mode
  (`localOnly`/`localAndRemote`/`remoteOnly`) and payment mode — admin/bartender must stay
  reachable regardless of whether customers get a remote path. `localOnly` = hard lockout (404/503)
  of the 4 customer-exclusive endpoints only, at every layer including the relay — never folded
  into a shared "is this LAN" check (iOS once had this bug; Android/relay never did).
- `effective_stripe = stripe_enabled and kiosk_mode != "localOnly"` (relay `require_approval`,
  Android `effectiveStripeEnabled`, iOS `AppState.effectiveStripeEnabled`) — the raw toggle value
  is preserved and always shown to the operator; only *behavior* reads the effective value. The
  actual kiosk button-visibility gate must be checked directly on each platform, not inferred from
  a property others "should" have deferred to.
- Stripe stays **visible but disabled** (not hidden) in Local Only mode, with an explanatory
  caption, on every admin/wizard surface.

**Requests & payments:**
- `SongRequest.price` and `payment_method` (`"free"|"bartender"|"stripe"`) are frozen at creation,
  never recomputed from current bar settings. A bartender's Approve tap **is** the payment
  confirmation for the bartender-pay path (`paid=True`, `payment_method="bartender"`).
- Single ISO currency field for both bartender cash display and Stripe.
- "Cancel" on Up Next (`bartender_deny()`) only works on `payment_method == "free"` requests,
  unconditionally (checked regardless of status, since a Stripe request's `payment_method` is set
  at creation, before `status` even reflects it).
- Relay's `bartender_requests()` computes a **display-only** status (a raw-`"pending"` Stripe or
  pure-auto-accept request reads as `"approved"` to the client) so the UI doesn't show a
  review-queue row for something that will never need review — raw stored status is untouched.
- `new_requests` can't distinguish a host's own echoed-back request from a genuinely foreign one —
  any consumer must adopt by checking local existence (e.g. `LocalRequestManager.getRequest(id)`),
  never a separately-tracked "have I seen this" set (Android had this bug; iOS's file-keyed upsert
  never could).
- Auto-accept is a mode, not a timer — no `auto_accept_minutes` fallback exists anywhere.
- Payment labels everywhere: "Stripe 💳", "Pay to bartender", "Auto (free requests)". "Both off:
  all requests auto-accepted for free" is unconditionally visible (not reactive-only) on every
  Payments surface.
- Pausing implicitly blocks **new** request submission for as long as it's paused
  (`effective_accepting_requests = accepting_requests and is_playing`), on every transport
  (relay + both LAN hosts + kiosk buttons) — replaces a deleted 30-min idle-pause
  auto-rotate/re-pause mechanism. Never applied to an already-succeeded Stripe payment, and never
  used for the raw `accepting_requests` echo (admin toggle always shows the operator's true
  configured preference). No refund-reconciliation mechanism exists anywhere (matches every other
  manual/cash-by-staff refund path in this system).
- **Auto-manage mode for `accepting_requests`** (2026-08-28, all 13 surfaces): Manual (existing
  toggle) or Auto (two watermarks, default max=10/restart=5, both-zero=inert), mutually exclusive
  and applied immediately on any change. Wire contract: `auto_manage_requests`/`auto_manage_max`/
  `auto_manage_restart`, same self-healing echo pattern as other settings. Evaluation is entirely
  host-side. "Outstanding" for the watermark = approved/up-next only, **excluding pending** (closes
  a no-show-spam abuse angle). A separate, complementary **per-requester pending-song throttle**
  (`MAX_PENDING_SONGS_PER_REQUESTER = 2`) blocks a requester at 3+ pending, non-Stripe songs,
  matched by the union of source IP and `customer_id` — applied only at the 3 anonymous
  network-request entry points, never at kiosk-native or post-payment endpoints.
- Per-requester/auto-manage UI went through several iterations before landing on a two-position
  slide "knob" (not a boolean toggle) for Manual/Auto, because a plain on/off switch kept implying
  one position was "disabled" when neither is. Render's settings controls are now fully optimistic
  (flip immediately, gated by `settings_pending` to avoid a flip-back-then-forward flicker).

**Bartender PIN / sessions / security:**
- Admin and bartender have independent PINs/secrets; bartender PIN is optional, empty = bartender
  role off entirely (404/503 everywhere, no QR, pairing rejected pre-comparison). PIN comparison is
  SHA-256 on all three surfaces for both fields (Android's admin PIN was plaintext until
  2026-08-08, since fixed with a one-time migration for existing installs).
- **Bartender Sessions tab** (remote admin only, not kiosk-native): Active Sessions (Kill, via an
  opaque `session_id` distinct from the real bearer token — closed a LAN credential-exposure gap
  the same way) + "Waiting to Retry" (a courtesy un-stick tool, explicitly reframed away from
  "security/attacker tracking" language after user pushback — the underlying per-IP throttle is
  unchanged and still valuable). Requires a true **admin** token, stricter than the bartender
  token, for all three endpoints.
- Bartender names must be unique among currently-active sessions (case-insensitive, checked at
  pair time on all 3 backends) and required, minimum 2 characters — closes a real
  who-do-I-Kill ambiguity. Sessions list sorts earliest-sign-in-first (iOS had a latent
  unordered-filesystem-listing bug here; Android/relay were already correct or fixed to match).
- Turning off bartender access now immediately purges existing paired sessions on all 3 backends
  (previously they stayed alive indefinitely) and the bartender page returns a friendly styled 404
  instead of a bare JSON blob when access is off. Tab-close detection is confirmed not solvable
  (no reliable browser signal) and intentionally not attempted.
- Changing the admin PIN now purges all previously-issued admin tokens on both host platforms
  (closes a stale-session-survives-PIN-reset gap); an Android LAN foreground-service lifecycle bug
  (`onTaskRemoved()` missing, `START_STICKY`) was the likely root cause of a stale LAN admin/
  bartender session surviving an app swipe-away, and was fixed directly.
- Bartender pairing has **no automatic expiration** — confirmed intentional; only End Session or an
  explicit Kill revokes it.
- Both LAN and render surfaces gained bartender/admin session-kicked handling (a stale/revoked
  token now produces a visible re-login prompt instead of silently failing every action or showing
  a misleading "ended by admin" message for a session that was never actually live).

**Kiosk lock-down (Android Device Protection / iOS Guided Access):**
- Android: `startLockTask()` (Screen Pinning) for real enforcement in `localOnly`/`localAndRemote`,
  plus `DevicePolicyManager.lockNow()` from `Activity.onStop()` ("Device Protection", requires
  lightweight Device Administrator consent, not full Device Owner/MDM) whenever the kiosk is live
  in **any** display mode including `remoteOnly` — forces the OS's own lock screen the instant the
  app leaves the foreground, for any reason. Has its own non-blocking wizard step. An earlier
  polling-based "detect an unpin and show a PIN-recovery screen" layer was built, misfired twice,
  and was **removed entirely** in favor of Device Protection alone (a plain lifecycle callback beats
  polling `lockTaskModeState` on a timer). Screen Pinning re-asserts itself on Admin-panel close and
  every `onResume()`.
- iOS has **no programmatic equivalent at all** for either capability — Guided Access is
  user-triggered only (triple-click gesture, no app-startable API), and Apple exposes no
  third-party device-lock API outside Supervised+MDM. The app can only detect/warn
  (`UIAccessibility.isGuidedAccessEnabled`) and has its own non-blocking wizard step for it. This
  asymmetry is a hard platform ceiling, not an inconsistency to fix.
- MDM (Jamf/Intune/Apple Business Manager) is the only path to a genuinely unbreakable kiosk on
  either platform — explicitly out of scope as a business/procurement decision, not a code gap.

**Reports:**
- Unified generation logic (`ReportManager`/`archiveSession`) used by every trigger (kiosk button,
  LAN admin button, session-end, remote relay-queued trigger). Played+denied+unfulfilled requests
  are written to the CSV **and deleted from live memory**; pending+approved get a snapshot, never
  deleted. No-op gate: nothing written if there's nothing played/denied to report. Retention:
  newest 20 files kept locally (by mtime).
- Relay mirrors report files (`BarSession.reports`) purely so an internet-transport admin can pull
  a report without LAN/physical kiosk access — not a durability/backup mechanism. Downloading a
  report from render **is** the cleanup action (no separate delete endpoint).

**Other standing facts:**
- `STRIPE_MINIMUMS` is a curated list, not the complete Stripe currency list.
- `stripe_enabled` is derived from key presence only when not explicitly sent
  (`body.get("stripe_enabled", bool(pk))`).
- Android's Spotify-failure handling (2026-08-08) never silently skips a song indefinitely: one
  `consecutiveSpotifyFailures` counter covers both startup and mid-song failures; 1st failure marks
  a **paid** request's remaining songs `UNFULFILLED` and advances; 2nd consecutive failure (any
  type) trips a full-stop PIN-gated outage screen. Counter resets only on an actual successful
  Spotify play, deliberately not inside a reconnect attempt.
- Android's queue-exhaustion reshuffle keeps not-yet-played Up Next requests in their existing
  relative position, only shuffling the filler portion — no dedup between the two groups (accepted
  as a minor, non-engineered-around cosmetic edge case). iOS has no equivalent (native opaque
  shuffle).
- 13-UI-surface parity passes (admin/bartender/customer × kiosk/LAN/internet × iOS/Android) found
  and fixed numerous small divergences through mid/late August 2026 — section ordering, copy
  accuracy (e.g. Android's "Local Only" wizard copy falsely implying forced auto-accept), header
  truncation, keyboard-dismiss gaps, nav-bar pinning/insets, switch styling, QR-code availability.
  See the archive for the full list if auditing a specific surface for parity.
- Practical lesson recorded from a live-testing session: **avoid pushing to jukebarweb's `main`
  while the user is actively live-testing** — Render auto-deploys on every push, which restarts the
  process and wipes all in-memory `BarSession` state (bartender tokens included), and this was once
  mistaken for a client-side session bug.

## Planned next
- Song counts from iOS/Android on register: `artists: [{name, song_count}]` instead of `[String]` — improves pie chart accuracy
- Stripe live key: apply under own business account to validate payment flow end-to-end before bar rollout
- Apple Pay domain file is served; needs Stripe dashboard domain registration to activate
- LLM fallback for obscure artists (no Last.fm data): deferred — needs API cost/rate infrastructure first
- Recommended playlist pipeline: deferred (see `docs/render_spec.md`)
