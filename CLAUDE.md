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

**Governing principle (2026-07-20): every surface writes to host app state; host app state broadcasts**
**itself out to every client.** This applies uniformly to settings, `now_playing`, `up_next`, and song
requests — regardless of which client (kiosk, LAN web, internet web, bartender, admin) originated the
change. A client action never updates relay state directly and has that stand as truth; it must change
the host's own state (directly if it's already host-local, or via a relay-queued action the host applies
on its next sync), and the host's next sync unconditionally re-asserts its full current state to the
relay — the same self-healing, no-version-numbers-needed pattern already used for settings (see below).
Two channels ride the same 5s heartbeat: **upstream** ("requested stuff" the host needs to know about
and make official — `new_requests`, `actions`, `desired_settings`, all delivered in the sync *response*
since the relay can't push to a polling host any other way) and **downstream** ("confirmed stuff" the
host broadcasts out unconditionally every tick — `settings` echo, `now_playing`, `up_next`, and now
`requests` too). A request is *born* wherever a customer/bartender/kiosk acts, but only becomes official
once it's host state; from there it propagates out to every reader (LAN pages read host state directly,
no relay hop; relay-served pages read the mirrored copy `host_sync()` maintains). Stripe fits this
without special-casing: the relay is simply the only thing Stripe can talk to, so a confirmed payment
just joins `new_requests` upstream like any other newly-known request.

**`bar.requests` full compliance, landed 2026-07-20:** `host_sync()`'s `requests` field carries the
host's complete local request list (any status) every call; the relay **upserts** each into
`bar.requests` — never deletes, since a customer-web/Stripe request the relay just created may not be in
the host's echo yet (not yet adopted), and history the host has stopped keeping locally shouldn't vanish
just because it aged out of the echo. For an id the relay already knows (born relay-side via
`bar_request()`/`bar_payment_confirmed()`), only `status`/`jump`/`approved_at`/`paid` are trusted from
the host — `price`/`payment_method`/`requester_name`/`song_ids` stay whatever the relay originally
recorded, since that's authoritative for anything born there, not something the host reconstructed.
`new_requests` (upstream) also now carries `price`/`payment_method` so a host adopting a relay-born
request doesn't have to guess them (iOS previously recomputed price from *current* bar settings here,
missing 3-song-bundle pricing — now trusts the relay's frozen value directly). This retired the
free-mode-only kiosk-POSTs-directly patch from earlier the same day: kiosk (iOS `CatalogBrowseView.submit()`,
Android `MainActivity.handleLocalRequest()`) now **always** just saves to host-local state
(`LocalStorage`/`LocalRequestManager`) regardless of network mode or approval mode — one path, no
relay-awareness at request-creation time at all, matching how LAN mode always worked. **iOS is fully
unified**: `LocalStorage` was already the single request store for every origin, so the old
`played_request_ids` mechanism (and its `pendingPlayedIds` bookkeeping) was deleted outright, fully
superseded by the `requests` echo. **Android is now fully unified too, as of the same day's follow-up**:
`LocalRequestManager` — previously only for kiosk/LAN-web-originated requests — is now also where
`RelayService.handleSyncResponse()` adopts internet-web/Stripe requests (`addRequest(requestId = req.id,
...)`, keeping the relay's own id so later approve/deny actions can find it), and where bartender
approve/deny actions and played-detection (`nowPlayingJob`'s song-changed check) write, replacing the old
`pendingRequests`/`PendingRequest`/`approvePending()`/`denyPending()` bookkeeping (all deleted — confirmed
unused by any UI before removal). `played_request_ids`/`approved_request_ids` are gone from both the
Android client and `host_sync()`'s handling — the `requests` echo alone now covers status for every
origin on both platforms. `RelayService` still keeps one small piece of its own bookkeeping,
`injectedRequestIds: Map<songId, requestId>` — that's a live-queue-position question (which currently
playing song belongs to which request, for `up_next`'s display and for pulling a cancelled song back out
of the ExoPlayer queue) that a flat request list doesn't answer, not request *state*, so it stays
separate from `LocalRequestManager` by design, matching how iOS's `PlaybackCoordinator`/`AppState` queue
tracking is also kept separate from `LocalStorage`. `LocalRequestManager.markPlayed()` also gained the
same "only the request's last song" guard iOS's `markRequestPlayed()` already had — previously it flagged
a whole multi-song request as played the moment its first song started.

**Settings propagation, single-slot design (replaces both the old optimistic-apply hack and a**
**short-lived versioned-echo design):** an admin/bartender toggle on `admin.html`/`bartender.html`
POSTs to `/api/bar/{id}/settings`, which writes the desired value into
`BarSession.desired_settings[field]` — a single latest-write-wins slot per field, not a queue. It does
**not** touch `bar.stripe_enabled`/`bartender_enabled`/`accepting_requests` directly. The host picks up
`desired_settings` in every `/api/host/sync` response, applies it locally, and its **own current
values** — sent unconditionally on every sync call regardless of who changed them or why — are what
actually update the live field on the relay (`bar.field = echo.value`, no version/ordering guard: the
host just re-asserts its truth every 5s, so any single dropped or out-of-order call self-heals on the
next tick). A `desired_settings` entry clears itself once the host's echo matches it; if a newer
request overwrote it in the meantime, the echo won't match and it simply keeps re-sending the newer
value. Client toggles stay locked (dimmed) exactly as long as their field name appears in
`settings_pending` (`= list(bar.desired_settings.keys())`).

**Register is never used for a routine toggle** — only for startup, a genuinely new session (End
Session/restart), or a catalog refresh. `host_register()` updates an existing `BarSession` in place
when it's the same ongoing session (same `jukebar_id` + `session`), rather than replacing it, so it
never wipes `now_playing`/`up_next_queue`/pending requests — but the real fix for that class of bug was
removing register from the toggle path entirely: a settings change is decoupled by construction from
playback-display state, because they now flow through completely separate fields with no shared
replace-the-whole-object step.

Covers three fields today: `stripe_enabled`, `bartender_enabled`, `accepting_requests`. **Any new
admin/bartender toggle needs**: `bar_settings()`'s field tuple, `host_sync()`'s echo/clear logic, the
`/api/bar/{id}/requests` response, both relay HTML files' Actions tab, iOS `AppState.swift`'s
`settingsEcho`/`applyDesiredSettings()` + `LocalServer.swift` (`/api/admin/settings`, LAN-only, direct
apply, unrelated to this mechanism) + both LAN HTML files, and Android `RelayClient.kt`/`RelayService.kt`
equivalents + `LocalServer.kt` (`handleAdminSettings`) + both LAN HTML files. Missing one of these
silently breaks only that surface, which is exactly how `accepting_requests` went unnoticed for ten
days before this design existed.

(2026-07-18: this replaced a same-day earlier version of this section describing per-field version
numbers and a `settings_pending`/`settings_version`/`settings_confirmed_version` triple. That design
worked but was more machinery than the problem needed — the version numbers were insurance against
network-reordering that the heartbeat's own repetition already bounds to one cycle, and the queue-style
`pending_actions` entry for settings updates was redundant with a plain latest-value slot.)

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

**`SongRequest.price` is frozen at creation time, not recomputed live** — mirrors iOS's `SongRequest.price: Double` (`let`) and Android's `LocalRequest.price: Double` (`val`), both already immutable. `bar_request()`/`bar_payment_confirmed()` compute it once via `_compute_price()` at the moment the request is created; `admin.html`'s `requestCard()` reads `r.price` rather than recomputing from the bar's *current* `price_per_song`/`price_for_three` — otherwise historical (played/denied) rows would show today's pricing instead of what was actually charged/quoted.

**Single currency field** — one ISO currency code for both bartender cash display and Stripe processing. No separate "display currency" vs "Stripe currency".

**`SongRequest.payment_method` (added 2026-07-18): `"free"` | `"bartender"` | `"stripe"`** — `paid`
(bool) is true for both `bartender` and `stripe`, false only for `free`. A bartender tapping Approve on
a pending request **is** the payment confirmation (cash/card collected in person) — `bartender_approve()`
sets both `paid=True` and `payment_method="bartender"` unconditionally, since this endpoint only ever
fires for the human-review path (Stripe-paid requests auto-inject via `new_requests` without ever
needing an approve tap, so there's no ambiguity). Mirrored on both host apps' local approval handlers
(iOS `AppState.swift`/`LocalServer.swift`, Android `LocalRequestManager.approveRequest()`) so their own
local `paid` flag agrees with the relay's. `admin.html`/`bartender.html`'s Up Next list renders this as
💳 (stripe) / 💵 (bartender) / a muted "Free" badge — the relay-side field alone drives that display,
since it's built from `bar.requests` which `bartender_approve()` mutates directly; no host-side
`payment_method` plumbing was needed for the admin display to work. Android's relay-mode Up Next echo
to the customer-facing `/api/bar/{id}/display` endpoint used to always send `paid: false` regardless of
reality — fixed same day by adding `PlaybackCoordinator.requestedSongPaid: Map<String, Boolean>`
alongside the existing `requestedSongNames` map, threaded through all 7 `injectSongs()` call sites.

**"Cancel" button on Up Next (added 2026-07-18)** — bartender/admin can pull an already-approved **free**
request back out of the live queue; paid ones (Stripe or bartender) cannot be cancelled this way at all.
Reuses the existing `POST /api/bar/{id}/deny` endpoint/action rather than a new one — `bartender_deny()`
rejects (403) whenever `payment_method != "free"`, unconditionally regardless of status. This has to be
unconditional, not just "once approved": a Stripe request's `payment_method` is set to `"stripe"` at
creation and never changes, so it needs blocking even during the brief window it's still raw
`status == "pending"` (not yet host-confirmed) — a status-gated check alone would have missed that
window. A still-pending *bartender-flow* request is unaffected (its `payment_method` only becomes
`"bartender"` at actual approval time, so it reads `"free"` until then and stays deniable pre-approval,
same as always). The host-side "pull it out of the live queue" mechanism already existed on iOS
(`MusicService.cancelRequest()`, built for LAN mode but never wired to the relay-driven `"deny"` action)
— just needed connecting. Android had no equivalent at all; added `PlaybackCoordinator.cancelRequestedSongs()`,
which only ever removes songs strictly after the current queue position (never touches the
currently-playing song) and skips immediately via `advance()` if one of the cancelled songs already lost
the race and became current — mirrors iOS's "let it flash then auto-skip" behavior for that same edge
case. Both platforms wired this into their LAN deny handler too (Android's LAN deny previously did
nothing beyond marking the status, another gap fixed as part of this).

**"Requests" vs "Up Next" display status is computed, not raw storage status (fixed 2026-07-18)** —
`bar.requests[rid].status` legitimately stays `"pending"` on the relay until the host's own `up_next`
echo confirms it (see `host_sync()`) — that's also what makes it appear in `new_requests` for the host
to pick up at all, so it can't just be flipped to `"approved"` at creation without breaking delivery.
But a Stripe-paid or pure-auto-accept-mode request was never actually going to need a bartender's
approve/deny tap, so showing it in the pending section with action buttons for that in-between window
was pure noise (and confusing — it would "pop down" to Up Next moments later on its own). `bartender_requests()`
now computes a display-only status: a `"pending"` request reads as `"approved"` to the client whenever
`payment_method == "stripe"` or the bar's `require_approval` is currently false (pure auto-accept mode)
— covers both "always skips review" (Stripe) and "this bar isn't reviewing anything right now" (free
mode). The raw stored status is untouched, so `new_requests`/host-confirmation logic is unaffected. This
is relay-only — LAN mode never had this bug, since both host apps already compute the correct status
synchronously at request-creation time with no async confirmation round-trip in between.

**LAN admin.html gets the same payment_method badges + Up Next rename (added 2026-07-18)** — both host
apps' LAN admin pages had the identical misleading-badge bug the relay pages had before today's earlier
fix (`r.paid ? 💳 : ''` — showed a card icon for cash-paid requests too), plus their approved/queued
section was still labeled "Approved — in queue" instead of "Up Next", and had no per-request Cancel for
free ones (the `actions === 'cancel'` branch existed in the JS but was dead code, never invoked). Both
host apps' local request models gained a `paymentMethod`/`payment_method` field mirroring the relay's
(`Models.swift`'s `SongRequest`, Android's `LocalRequestManager.LocalRequest`), set at the same points as
the relay version: `"stripe"` at Stripe-payment creation, `"bartender"` at the moment a bartender
actually approves (both LAN's own approve endpoint and the relay-driven approve action), default
`"free"`/nil otherwise. Both LAN `deny` endpoints also gained the same `payment_method != "free"` 403
gate as the relay's `bartender_deny()`, for the same reason — a Stripe/bartender-paid request must never
be cancellable through this one-click action, on any surface. LAN `bartender.html` on both platforms only
ever shows *pending* requests (no Up Next section exists there at all), so nothing needed changing there
— left as a known scope boundary, not silently skipped.

**"Past Requests" overlay for played/denied history (added 2026-07-19, relay `admin.html` only)** — the
relay's `bartender_requests()` (the live Requests/Up Next feed) has always excluded `"played"`/`"denied"`
entries by design (`if r.status in ("pending", "approved", "approved_jump")`) — that data was never lost,
it just had nowhere to render: the only unfiltered endpoint, `bar_history()`, was consumed solely by the
Reports tab for a numeric count, never an inline list. Rather than broaden the live feed (and permanently
crowd the screen with history), added a "📜 Past Requests" button that opens a bottom-sheet overlay,
fetching `bar_history()` on demand — nothing loads until asked for. `bar_history()` was enriched to
return the same shape as `bartender_requests()` (`song_details`, `payment_method`, etc.) so the existing
`requestCard()` renderer could be reused as-is. That reuse surfaced a latent bug: `requestCard()`'s
non-actionable branch **hardcoded "In queue" regardless of actual status** — harmless before, since it
was only ever called with genuinely-approved rows, but wrong the moment played/denied rows started
flowing through the same renderer. Fixed to branch on `r.status` properly (`.req-badge.played`/`.denied`
added). **LAN admin.html on both platforms got the equivalent restructuring**, but simpler: their
`/api/requests` was never filtered by status to begin with (`renderRequests()` already received
everything and did its own client-side grouping into Pending/Up Next/Played/Denied, always rendering all
four inline) — so no backend change was needed, just moving the already-correct Played/Denied rendering
out of the main scroll and into the same on-demand overlay pattern, reusing already-fetched data with no
extra network call.

**STRIPE_MINIMUMS is a curated list** — not the complete Stripe currency list. Only currencies we actively support with known minimums.

**`stripe_enabled` derived from key presence on register** — relay register handler originally derived `stripe_enabled = bool(pk)` (key present = enabled). Now uses `body.get("stripe_enabled", bool(pk))` so host can explicitly disable Stripe while keeping the key stored. Android client sends `stripe_enabled` explicitly; iOS sends empty key when disabled.

**Auto-accept is a mode, not a timer** — `auto_accept_minutes` is gone from all surfaces. There are three approval modes: Stripe payment required, pay-to-bartender, or auto-accept (free requests, no approval). No timer fallback exists or is planned.

**Payment labels (all admin pages):** "Stripe 💳", "Pay to bartender", "Auto (free requests)"

## Planned next
- Song counts from iOS/Android on register: `artists: [{name, song_count}]` instead of `[String]` — improves pie chart accuracy
- Stripe live key: apply under own business account to validate payment flow end-to-end before bar rollout
- Apple Pay domain file is served; needs Stripe dashboard domain registration to activate
- LLM fallback for obscure artists (no Last.fm data): deferred — needs API cost/rate infrastructure first
- Recommended playlist pipeline: deferred (see `docs/render_spec.md`)
