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

**Kiosk display mode is orthogonal to transport (fixed 2026-07-22)** — three independent axes
govern a session: transport (`wifi`/`hotspot`/`internet`, always one of these — see below),
customer-facing kiosk mode (`localOnly`/`localAndRemote`/`remoteOnly`), and payment mode
(free/bartender-pay/Stripe). Before this date, picking "Local Only" in the setup wizard
incorrectly set a fourth, since-removed `PreferredNetworkMode.local`/`networkMode="local"` value
that skipped choosing a transport entirely — meaning `LocalServer` never started and no relay
registration happened, so **admin and bartender had no way to be reached at all**, even though
approvals can be needed any time payment mode changes to bartender-pay. Root cause: "no remote
customer access" and "no network for anyone at all" were conflated into one flag. Fixed by
retiring the standalone local-only transport value — a transport is now **always** chosen via the
network-picker wizard step, for all three kiosk display modes, since admin/bartender need one
reachable at all times regardless of whether customers get one. `kioskDisplayMode == .localOnly`
now means exactly one thing: no customer-facing QR, and the local Request button is the only way
customers can submit — a deliberate **hard lockout**, not just an unadvertised URL, since the
reason an operator picks it is often "someone's been abusing the QR from next door." Enforced at
every layer that could otherwise still accept a customer submission: both hosts' `LocalServer`
customer page/write endpoints (`/api/request`, `/api/create-payment-intent`,
`/api/payment-confirmed`, `/api/request/:id`, and the customer page route itself) return a real
404/503 — not a friendly error page — and so does the relay's own customer surface
(`bar_page()`, `bar_request()`, `bar_request_status()`, `bar_create_payment_intent()`,
`bar_payment_confirmed()`) for the internet-transport case, via a new `BarSession.kiosk_mode`
field sent once at `host_register()` time (kiosk mode is fixed for a session's lifetime, only
changeable via End Session + re-setup — no live toggle, so no `host_sync()` echo needed). Stripe
stays **visible but disabled** (not hidden) whenever `kioskDisplayMode == .localOnly`, in both the
setup wizard and the live Admin screen (iOS `SetupView.swift`/`AdminView.swift`, Android
`ApprovalModeStep.kt`/`AdminScreen.kt`) — a caption explains why ("no customer page exists to pay
from"). First cut just left it fully enabled with no visual difference, which the user flagged
after testing as actively confusing: a setting that's shown as freely toggleable but silently has
no effect looks broken, not discoverable. Disabled-but-visible is the middle ground — the operator
learns Stripe exists without being misled that flipping it does anything right now.
`KioskDisplayMode.localRemote` was
also renamed to `.localAndRemote` (Android: `"localRemote"` → `"localAndRemote"`) for clarity —
zero relay involvement in that rename, kiosk display mode was never part of any wire payload
before this fix.

**Stripe's stored value must never be read raw for behavior once `kioskDisplayMode == .localOnly`
exists (follow-up fix, same day)** — disabling Stripe's toggle in Local Only mode (previous
decision) only stops the *user* from flipping it; the underlying `stripeEnabled`/`stripe_enabled`
value stays whatever it was before the mode switch, by design, so the operator's preference
survives switching back later. But that means anything computing "does this bar need approval /
is Stripe an active payment method" from the raw value gets it wrong the moment Bartender Pay is
also turned off: `stripeEnabled=true, bartenderEnabled=false` reads as "still needs approval" even
though Stripe can't actually be used — hiding the kiosk's own Request button entirely, the same
symptom as `accepting_requests` being off, when the bar is actually free/auto-accept. Fixed by
introducing one **effective** value per platform that both discounts Stripe whenever `kiosk_mode
== "localOnly"`: relay `BarSession.require_approval` (the single property everything already
funneled through) now computes `effective_stripe = stripe_enabled and kiosk_mode != "localOnly"`
before ORing with `bartender_enabled`. Android: `BarDetails.effectiveStripeEnabled`/`requireApproval`
fixed the same way, in one place, since `kioskMode` already lived in the same struct as
`stripeEnabled` — every consumer (`MainActivity.handleLocalRequest()`, `RelayService`'s
auto-approve) already read through `requireApproval`, so fixing the property fixed all of them for
free. iOS needed a new `AppState.effectiveStripeEnabled` computed property (since `kioskDisplayMode`
lives on `AppState`, separate from `HostConfig`) threaded manually into every behavioral call
site — and the *first pass at that (still same day) missed the actual kiosk button-visibility
gate*, `KioskView.swift`'s `allowsLocalRequest`, which reads `cfg?.stripeEnabled` directly rather
than going through `AppState` at all — exactly the bug the user then hit and reported. Lesson: when
sweeping raw-value consumers on a platform with no single natural choke point, grep isn't enough —
the actual UI-visibility gate has to be checked directly, not inferred from the properties it
should have deferred to. Register/sync payloads and admin toggle UI display still intentionally
read the **raw** stored value everywhere (never the effective one) — that's what keeps the
toggle showing the operator's true preference, greyed out, rather than silently flipping it off.

**The `kioskDisplayMode == .localOnly` hard lockout must never be folded into a check shared with
admin/bartender routes (follow-up fix, same day)** — iOS's `LocalServer.swift` has one
`checkLocalMode()` function used by all 15 LAN routes: the 4 genuinely customer-exclusive ones
(`/api/request`, `/api/create-payment-intent`, `/api/payment-confirmed`, `/api/request/:id`), but
also every admin-facing route (`/api/request/approve`, `/api/request/deny`), every
bartender-facing one (`/api/bartender/pair`/`status`/`list`/`approve`/`deny`), and shared ones
(`/api/catalog`, `/api/requests`, `/api/genres`, `/api/init`) — it was originally built solely for
wifi/hotspot IP-range isolation, nothing customer-specific about its name. The first version of
the Local-Only lockout added `guard kioskDisplayMode != .localOnly else { return false }` directly
inside `checkLocalMode()` itself, which meant **every one of those 15 routes** started rejecting
in Local Only mode — admin.html could no longer even list pending requests (`/api/requests`),
bartenders couldn't pair or approve/deny anything, none of it just the 4 routes that were actually
supposed to be locked out. Symptom the user hit: "requests made to bartender do not show up on
the WiFi admin page" — not a request-visibility bug at all, but the entire admin/bartender LAN
surface silently broken. Fixed by splitting the lockout into its own `checkCustomerAllowed()`,
applied only at the 4 customer-exclusive routes; `checkLocalMode()` reverted to its original sole
job. Android and the relay never had this bug — both already kept an equivalent narrow check
(`LocalServer.kt`'s `checkCustomerAllowed()`, main.py's per-endpoint `kiosk_mode` checks inside
`bar_request()`/`bar_payment_confirmed()`/etc., never folded into the shared `_customer_bar()`
used by 13+ endpoints) separate from day one. Lesson: before adding a hard lockout to an existing
shared helper, grep every call site first — a name like `checkLocalMode` doesn't tell you its
actual blast radius.

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

**`host_sync()`'s `new_requests` cannot tell a host's own echoed-back request from a genuinely**
**foreign one (fixed on Android 2026-07-22)** — `new_requests` is built purely from `bar.requests`
filtered to `status == "pending"` (see `host_sync()` docstring), with no origin tag distinguishing
"born on the relay via customer-web/Stripe" from "born on this very host (kiosk/LAN) and already
echoed up via the `requests` field on an earlier tick." A host that naively adopts every entry in
`new_requests` as if it might be new will, the first time its own kiosk/LAN request comes back down
still pending, try to adopt an id it already has locally. Android's `RelayService.handleSyncResponse()`
did exactly this, gated only by a volatile in-memory `seenRequestIds` set — `LocalRequestManager.addRequest()`
has no id-uniqueness check, so this created a second `LocalRequest` object sharing the same `requestId`,
permanently stuck at `PENDING` (approve/getRequest only ever resolve the *first* match). Since both
copies get serialized into every later sync's `requests` array, and `host_sync()`'s upsert loop applies
them in array order, the stale duplicate (sorted after the original) kept overwriting the just-approved
status back to `"pending"` on every tick — symptom: admin's Requests/Up Next split never happened, the
request just sat at the bottom of one list forever showing "Sent" after being tapped, while playback
(a separate, already-correct path) proceeded normally and Reports stayed empty since the request never
reached "played" server-side either. Fixed by gating adoption on `localRequestManager.getRequest(id) !=
null` (a durable, accurate "do we already know this" check) instead of the separate volatile set, which
was removed outright. **iOS doesn't have this bug** — `LocalStorage.saveRequest()` writes to a file
keyed by request id, a true upsert, so iOS reprocessing its own echoed-back request just overwrites with
equivalent data, never creating a duplicate object. Any future client-side consumer of `new_requests`
must adopt by checking local existence first, not a separately-tracked "have I seen this" set — the
relay has no way to help by tagging origin, since by design it doesn't track which host any given
request came from.

**Admin/bartender PIN split (2026-08-01), all three surfaces** — admin and bartender used to share
one PIN (`BarSession.pin_hash` / iOS `BarConfig.pinHash` / Android `BarDetails.pin`), found while
scoping the still-pending "Bartender Sessions" admin tab: rotating a shared bartender PIN would have
silently rotated the admin's own PIN too. Split into two independent secrets. **Bartender PIN is
optional and empty by default — not derived from or defaulted to the admin PIN** — when empty, the
bartender role is off entirely for that bar: no bartender QR/URL is shown anywhere, `/bartender/{id}`
(relay) and both hosts' LAN `/bartender` + `/api/bartender/pair` return 404/503, and pairing is
rejected without even attempting a PIN compare. Admin does approve/deny/control/settings directly
instead — nothing new needed there, that capability already existed. Admin sets/changes/clears the
bartender PIN in place, a genuinely new capability — previously PIN changes were wizard-only on both
host platforms (still true for the *admin* PIN, untouched). New "Bartender Access" control added to
every Admin surface: kiosk-native (iOS `AdminView.swift`, Android `AdminScreen.kt`), LAN admin.html
(both), and render `static/admin.html` (this repo).

Wire contract (relay `main.py`): `BarSession.bartender_pin_hash: str = ""`, sent by the host in
`host_register()`'s body and every `host_sync()`'s `settings` echo (now `dict[str, bool | str]`, not
`dict[str, bool]` — empty string is a meaningful desired value, not "field absent"), self-healing
exactly like the three bool settings. `POST /api/bar/{id}/authenticate` gained a `role` field
(`"admin"` | `"bartender"`, defaults `"admin"`) so it can check the right secret — `bar.pin_hash` for
admin, `bar.bartender_pin_hash` for bartender — since previously this one endpoint had no concept of
which page was calling it. Per-IP lockout is now keyed `(bar, ip, role)`, not just `(bar, ip)`, so
repeated bad bartender guesses from a shared bar IP no longer also lock out that IP's admin PIN.
`bar_settings()` accepts `bartender_pin_hash` in its body alongside the three existing toggle fields —
admin-only by UI convention (only admin.html exposes the control) but the endpoint itself doesn't
enforce that distinction, same as the three pre-existing toggles which bartender.html can already
post too.

**PIN comparison is hash-based (SHA-256) for the bartender field on all three surfaces, including
Android** — a deliberate departure from Android's existing admin-PIN comparison, which stays
plaintext (`BarDetails.pin`, untouched, pre-existing inconsistency vs iOS/relay not in scope here).
The bartender field had no existing plaintext-comparison precedent to preserve, and a hash keeps LAN
and render consistent: render's admin.html hashes client-side (has Web Crypto, served over https) and
sends a hash straight through; both hosts' plain-http LAN admin.html pages have no `crypto.subtle`
available, so they send the raw entered PIN over `/api/admin/settings` and the **host hashes it
server-side** before storing — both iOS and Android converged on this same approach independently.
Either way, only a hash is ever what's compared against or forwarded to the relay.

**Bartender Sessions admin tab (2026-08-02), on remote admin only — LAN admin.html both**
**platforms + render `static/admin.html`, deliberately NOT kiosk-native.** Surfaces data that
already existed as bookkeeping for other purposes (paired-bartender records, PIN-lockout attempt
counters) behind a new "Sessions" tab: an **Active Bartender Sessions** list (name, paired-at,
source IP, per-row **Kill**) and a **Waiting to Retry** list (last name attempted, IP, attempt
count, remaining wait time, per-row **Let Retry Now**). All three new endpoints/routes on every
surface require a true **admin** token specifically — deliberately excludes LAN's
isAdmin-promoted-first-bartender path (`isValidSettingsToken`/Android's equivalent), since
managing *other* bartenders' sessions and PIN lockouts shouldn't be something even the "admin
bartender" can do to peers. Relay: `_require_admin_token()` (stricter than the pre-existing
`_require_bartender_token()`), checking a new `role` field now stored per `bartender_tokens`
entry (minted with `role` at `bar_authenticate()` time, same call that already carries it from
the PIN-split work). `bar_settings()`'s `bartender_pin_hash` field was tightened to also require
admin token specifically, closing what was previously only a UI convention.

**Kill** revokes one bartender's credential immediately — their next call to any protected
endpoint 401s. Relay mints a separate opaque `session_id` per token (alongside the real bearer
token) specifically so admin.html never has to hold or display a working bartender credential to
list/kill sessions. **LAN has no equivalent separation** — a bartender's `bartenderId` (Android)
/ `BartenderRecord.id` (iOS) **is** its bearer token by existing design, so the LAN sessions list
necessarily exposes each bartender's actual working credential to the admin viewing it. Accepted
as a known, deliberate scope boundary rather than a bug to fix here — restructuring LAN's
identity/credential split is a real refactor, and LAN already requires physical network presence
(a different threat model than the relay's full internet exposure, the same reasoning already
applied elsewhere to other LAN/render asymmetries in this doc).

**Kill's confirm flow bundles a convenience**: "also change the bartender PIN so they can't just
re-pair" — reuses the existing bartender-PIN-set mechanism (the Actions-tab control from the PIN
split above) rather than being a separate feature; killing and rotating are still two independent
calls under the hood, just offered together in one UI flow since a hacker who already knew the
old PIN needs both to actually be locked out.

**Killed bartenders now get a clean client-side logout, not a silently-stuck UI**: none of the
three `bartender.html`/`WebApps/bartender.html`/`assets/bartender.html` poll loops previously
handled a 401 response at all — a revoked session would just poll into 401s forever with the UI
frozen on stale data. All three gained a `sessionKicked()`/equivalent: clears the cached
token/bartenderId and drops back to the PIN/pair screen with an explanatory message.

**Incidental bug found and fixed on Android during this work**: `LocalServer.kt`'s `jsonError()`
had no `401` case in its status-code `when` block, so all 4 pre-existing `jsonError(401, ...)`
call sites (approve/deny/list/settings auth failures) were actually returning HTTP 500, not 401 —
silently wrong for as long as the token-auth layer has existed. Fixed as part of wiring the new
endpoints' own 401s (which needed it to work at all), incidentally fixing the pre-existing ones too.

**Renamed "Locked-Out IPs" → "Waiting to Retry" the same day, after user pushback**: the original
framing implied a maintained blocklist of "malicious IPs" worth identifying/tracking — user
correctly pointed out this doesn't hold up: a determined attacker just retries from a different
IP (the lockout is per-IP, not global, by earlier deliberate design — see the PIN-split section
above), and once the PIN itself is changed in response to a real attack, any given IP's lockout
state is moot anyway. **The underlying per-IP throttle in `bar_authenticate()`/LAN pairing is
unchanged and still valuable** — it still stops the common case (one unthrottled script hammering
the endpoint) from brute-forcing a short numeric PIN in seconds, which "just change the PIN
reactively" can't help with if nobody's watching closely enough to notice first. What changed is
purely the *framing and purpose* of the admin-facing list: not a security/hacker-tracking view,
but a courtesy tool for un-sticking a legitimate bartender who fumbled their own PIN and doesn't
want to wait out the full cooldown ("bail Joe out" — the phrase from the original design
discussion for this exact action). Row copy changed from "locked"/"failed attempts" to "waiting"/
"attempts so far," and the action button from "Clear" to "Let Retry Now," across all three admin
surfaces (relay + both LAN admin.html files) — copy-only, no endpoint/field/behavior changes.

**Kiosk lock-to-app (2026-08-02), iOS + Android only — no jukebarweb involvement, wire-contract-free.**
Prevents casual escaping to device Settings/other apps while a kiosk session is live. Gated on
`kioskDisplayMode` being `localOnly` or `localAndRemote` only — `remoteOnly` means customers never
touch the kiosk screen directly (their own phones via QR only), so there's nothing to lock down for
them. **The two platforms are structurally asymmetric, and this can't be fixed in software**:
- **Android**: `Activity.startLockTask()` is callable by any app, no special device provisioning —
  real enforcement. `MainActivity.kt` reacts to `(isSetupComplete, barDetails.kioskMode)` via a
  `LaunchedEffect`, guarded against `IllegalStateException` by checking
  `ActivityManager.lockTaskModeState` before calling start/stop. Not unbreakable — exiting still
  needs a physical gesture (back+overview held, or swipe-up-hold depending on nav mode/OS version),
  optionally gated further by the device's own lock-screen credential if one is set. That's the
  ceiling of what's achievable without Device Owner provisioning (factory-reset QR/ADB enrollment) —
  deliberately out of scope, a deployment decision not an app change.
- **iOS has no programmatic equivalent at all.** Guided Access (the relevant Accessibility feature)
  can only be started by the *user*, manually, via a triple-click gesture each session — Apple
  provides zero API for an app to trigger it, only to read current status
  (`UIAccessibility.isGuidedAccessEnabled`) and observe changes
  (`UIAccessibility.guidedAccessStatusDidChangeNotification`). `KioskView.swift` detects and shows a
  small persistent warning caption (same visual slot as the existing `serverError` caption) when
  Guided Access isn't currently active, live-updating via the notification with no relaunch needed.
  True forced lockdown on iOS requires Supervised+MDM enrollment (Apple Business Manager or Apple
  Configurator) — same category as Android's Device Owner path, same reason it's out of scope here.
- **MDM** (Mobile Device Management — Jamf/Intune/etc.) is the enterprise provisioning layer both
  platforms' *true* unbreakable kiosk modes ultimately depend on. Neither this feature nor anything
  else in the app can substitute for it; it's a business/procurement decision for however bar owners
  buy and set up their hardware, not a toggle. Explicitly not pursued this pass.
- **Setup-wizard advisory, added same day, asymmetric by necessity**: Local Only / Local + Remote
  cards on both platforms' `DisplayModeStep.kt`/`DisplayModeGateView.swift` now recommend enabling
  the platform's lockdown mechanism first. **iOS's version is a real conditional check** (reads
  `UIAccessibility.isGuidedAccessEnabled` fresh each render, appends the warning only if it's off)
  since the API to detect it exists. **Android's version is a static recommendation, not
  conditional** — confirmed via research that no public API exists to query whether the user has
  pre-enabled Settings → Security → Screen Pinning (unlike Guided Access, this isn't something an
  app can detect; `ActivityManager.lockTaskModeState` only reports *current* pinned state, not the
  underlying Settings toggle). This asymmetry is a hard platform limit, not an inconsistency to fix.

**Android: empty admin PIN was a real, reachable full auth bypass — found and fixed 2026-08-02,
no jukebarweb involvement.** Traced end to end, not just theorized: `AdminPinStep.kt`'s
`canProceed` lets the operator leave the PIN field blank when re-running the wizard on an existing
bar (`hasSavedPin == true`), intending "leave blank to keep your current PIN." But
`SetupWizardScreen.kt`'s `onNext` for `WizardStep.ADMIN_PIN` did `barDetails.copy(pin = pin)`
unconditionally — an empty field meant `pin = ""`, which **overwrote** the real PIN with an empty
string rather than preserving it, silently breaking the promise the UI text made. That empty PIN
then fed straight into `KioskView.kt`'s `AdminPinEntry.verify()`, which had
`if (adminPin.isEmpty() || enteredPin == adminPin)` — once `adminPin` was empty, **any PIN
whatsoever unlocked the kiosk admin panel**. Fixed at both points: `SetupWizardScreen.kt` now
resolves a blank field to the existing PIN (`pin.ifEmpty { barDetails?.pin ?: "" }`) before saving,
so the "keep current PIN" promise is actually honored; `KioskView.kt` also dropped the
`isEmpty()` bypass entirely as defense-in-depth (fail closed, not fail open — an empty stored PIN
should mean "nothing matches," recoverable only via the existing device-biometric-gated "Forgot
PIN?" flow, never "anyone gets in"). **iOS never had this bug** — `SetupView.swift`'s
`advanceFromPin()` already left `pinHash` untouched (not overwritten) when the field was blank,
and `AdminView.swift`'s `verify()` has never had an `isEmpty()` bypass to begin with; Android's fix
brings it to parity with iOS's already-correct fail-closed design, not the other way around.

**Bartender pairing has no automatic expiration — confirmed intentional (2026-08-02), not a gap.**
A paired bartender's credential (Android `LocalBartender.bartenderId`, iOS `BartenderRecord.id` —
both double as their own bearer token) stays valid indefinitely until either End Session or the
admin manually revokes it via the Bartender Sessions tab's Kill action. The backlog item that
raised this compared it against "the customer session token, which does rotate mid-session" —
that comparison turned out not to hold up: that rotation is a 30-min-idle-pause-triggered
mechanism for invalidating stale customer QR codes/links, unrelated to credential security
lifetime, and (see below) has since been removed entirely anyway. A bartender pairs once per
shift and should stay usable for the whole shift; the actual "revoke a compromised/departed
bartender" need is already served by Kill. Adding auto-expiry would just log bartenders out
mid-shift for no real security gain, given LAN already assumes physical presence as the trust
boundary. No code change — confirmed as-is.

**The 30-minute idle-pause auto-mechanism was removed entirely (2026-08-02), both platforms —**
**not fixed, deleted.** Investigating the bartender-expiration question above surfaced it: on
both platforms, whenever playback transitioned to not-playing (any cause — manual pause,
whatever), a 30-minute timer armed; if still not resumed when it fired, iOS just re-paused
(itself a fix from earlier this same session, replacing an older version that used to also
rotate the session/QR and wipe the live queue) while Android rotated the session/QR (invalidating
old customer links) without touching the queue — the two platforms were themselves inconsistent
with each other. Its original purpose: stop an overnight session from staying live and acceptable
if the bartender went home without ending it. **User's call, after discussion**: this mechanism
is no longer needed at all, given the replacement below — "when the night ends, the admin should
put the app into End Session... if they fail to do that, it is on them. No need to trigger
[anything] if they accidentally did this through the Pause button." Fully deleted, not gated
behind a flag: iOS's `pauseTimerTask`/`hasEverStartedPlaying`/`stopPlaying()`/`startPauseTimer()`/
`cancelPauseTimer()` and the `MPMusicPlayerControllerPlaybackStateDidChange` observer that armed
it (`AppState.swift`); Android's `PlaybackCoordinator.onSessionTimeout`/`pauseTimerJob`/
`hasEverStartedPlaying`/`startPauseTimer()`/`cancelPauseTimer()`, plus the now-dead
`MainActivity.rotateSession()` and `LocalRequestManager.rotateSession()` it was the sole caller of.

**Replacement: pausing implicitly blocks new request submission, for as long as it's paused, no**
**timer, no escalation.** New `BarSession.effective_accepting_requests` property (relay, mirrors
the existing `require_approval`/`effective_stripe` raw-vs-effective pattern) =
`accepting_requests and is_playing`. Used in `bar_request()`, `bar_create_payment_intent()`
(blocks submission) and `bar_catalog()`'s echo to `customer.html` (hides/disables the Request
button proactively, not just a 403 after tapping) — catalog browsing itself is untouched.
Deliberately **not** used in `bar_payment_confirmed()` (an already-succeeded Stripe payment must
still be honored even if playback paused in the interim) or in the admin-facing
`bartender_requests()` response or `host_register()`/`host_sync()`'s `accepting_requests` field —
those keep echoing the raw stored value, so admin.html's toggle keeps showing the operator's
actual configured preference rather than getting silently flipped by pause state, same reasoning
as `effectiveStripeEnabled`'s raw/effective split. Both host platforms' local kiosk Request button
gained the identical gate (`MusicService.shared.isPlaying` / `coordinator.isPlaying`), so the
in-person kiosk button and the remote/QR path behave identically. No refund-reconciliation
mechanism exists anywhere in this codebase for a request that becomes stuck this way (confirmed
via grep — refunds are documented everywhere else as manual/cash-by-staff, never automated) — not
a gap introduced by this change, matches how every other refund scenario in this system already
works, and this new mechanism doesn't wipe or touch existing Up Next entries at all, only blocks
*new* submissions while paused.

**Gap found and closed the same week (2026-08-06): LAN transport was missed by the pause-blocks-**
**requests work above.** The first pass only touched the relay (internet transport) and the local
kiosk button — LAN-mode remote customers are served entirely by the host's own `LocalServer` and
never touch the relay at all, so they'd kept gating purely on the raw `acceptingRequests` value
with no play-state check. Closed by giving LAN its own copy of the same fix: iOS
`LocalServer.swift`'s `/api/nowplaying` echo, `/api/request`, and `/api/create-payment-intent`,
and Android `LocalServer.kt`'s `handleNowPlaying()`/`handleSubmitRequest()`/
`handleCreatePaymentIntent()`, all now require `isPlaying` alongside the existing
`acceptingRequests` check. Deliberately left untouched: both platforms' cached full-catalog
response (`preloadCatalog()`/`catalogJson()`) — baking a live value into a cache that only
rebuilds occasionally would go stale immediately, so the *live-polled* `/api/nowplaying` is where
LAN's effective gating has to live, mirroring the relay's own cached-catalog-vs-live-poll split.
Lesson for next time a fix spans "internet + LAN + kiosk-native": grep for the *other* two
surfaces before considering a fix done, even when the task at hand only mentions one of them.

**Android: setup now blocks launching a kiosk with nothing to play (2026-08-06), Android only —**
**iOS not investigated, out of scope for this item.** Skipping both the Spotify device step and
the local-folder step during setup (a two-tap path, no gating existed) used to complete setup
silently with an empty `coordinator.fullCatalog` — the kiosk would show a frozen "—" now-playing
tile with the Request button hidden, indistinguishable from a normal pause, no explanation
anywhere. Two fixes: (1) `SetupWizardScreen.kt`'s SUMMARY step (which reuses the same `AdminScreen`
composable as the *live* in-kiosk admin overlay) now checks `coordinator.fullCatalog.isEmpty()`
before calling through to `onLaunchKiosk`, showing a blocking `AlertDialog` instead — checked
*here*, not inside `AdminScreen.kt` itself, because `AdminScreen`'s `onDone` means something
completely different when shown live (`KioskView.kt`: just closes the admin overlay) — the guard
must only ever fire during setup completion, never when an operator is just dismissing the live
admin panel for an unrelated reason. (2) `KioskView.kt`'s now-playing tile (both portrait and
landscape layouts) now shows "No music to play" instead of the generic "—" specifically when
`fullCatalog.isEmpty()`, as a backstop for any other path that could still leave the catalog empty
(e.g. a selected Spotify playlist that turns out to have zero tracks — the wizard-level check
catches this too, since it looks at the actual resulting catalog size, not just "did you skip the
steps") — the normal "—" placeholder for an ordinary paused moment with a non-empty catalog is
unchanged.

**Report generation redesigned + mirrored to the relay (2026-08-07), all three repos — closes**
**backlog items 9/10/11 together, not separately.** Old behavior (both platforms had near-identical
independent implementations, clearly ported from one to the other, same bugs in both): silently
no-op on an empty session with zero feedback; dumped every request regardless of status (pending/
approved included, not just finalized); no dedup between runs (two reports in one session fully
overlapped); no file retention limit.

**New unified generation logic** (Android: new `local/ReportManager.kt`, extracted out of
`LocalServer` so `RelayService` — no `LocalServer` instance in internet-only mode — can trigger it
identically; iOS: `LocalStorage.archiveSession(sessionId:)`), used by every trigger (kiosk button,
LAN admin.html button, session-end teardown, and the new remote trigger below) with no third,
subtly-different path:
- **Played + denied requests** (both terminal/finalized) get written into the report, then
  **deleted from live memory** — a real deletion (`LocalRequestManager.removeRequests()` /
  `LocalStorage.deleteRequest()`), not just excluded from the report, so they also disappear from
  anything reading the same store (e.g. LAN admin.html's own Played/Denied history sections).
  Once reported, the CSV is the permanent record — deliberate, confirmed with the user.
- **Pending + approved/approved-jump** get a snapshot in the same report, every time, **never**
  deleted — still live/unresolved, the admin screen is where those are managed. Can legitimately
  reappear in a later report if still unresolved by then.
- **No-op gate**: exactly "are there any played+denied requests right now" — since they're deleted
  immediately after being reported, their mere presence already means "not yet reported," no
  separate tracking needed. Empty → no file written, explicit "No report created — no unreported
  requests yet" feedback on every UI trigger (previously identical visual feedback whether a report
  was actually created or silently skipped). A pending-only session with zero plays also produces
  nothing — not even the snapshot alone; the gate checks played+denied specifically.
- **Retention**: newest 20 report files kept locally, oldest deleted on any new generation
  (sorted by file mtime, not filename, so this stays correct regardless of naming) — unconditional,
  even if a file was never downloaded.

**New remote trigger + relay report mirror**, since render admin.html can't reach the kiosk
directly (that's the whole reason the relay exists): a new "Generate Report Now" button on render
queues a `{"type": "generate_report"}` action via the existing `pending_actions` delivery
mechanism (same as approve/deny/control), applied by the host on its next sync through the
identical `ReportManager`/`archiveSession` path as the two local buttons.

**Why the mirror exists, corrected**: NOT a data-durability/backup concern — reports living only on
the kiosk is already the normal, accepted case for WiFi/Hotspot/Local transport, same as everywhere
else in this system. It's specifically so an admin on **internet** transport can pull a report down
to their own phone/device without needing physical or LAN access to the kiosk at all — a reachability
problem, not a fragility one. The relay mirrors whatever reports exist on the kiosk (`BarSession.
reports: dict[filename, {content, created_at}]`, no cap of its own — the host's 20-file local
retention is the only real limit) so render can list/download without that LAN/physical dependency:
- Every `/api/host/register`/`/api/host/sync` call now also sends `report_filenames` (just names,
  cheap, full current list every call — same self-healing full-reassert pattern as
  settings/requests, not a delta) — `_reconcile_reports()` prunes anything cached that's no longer
  in this list (kiosk-local delete → relay's mirror drops it too, no separate propagation needed
  for that direction).
- Both endpoints' responses now include `reports_needed` — filenames the relay has no content
  cached for (after a relay restart, a genuine new session replacing the `BarSession` outright, or
  a dropped upload) — the host reads this and re-uploads via the new dedicated
  `POST /api/host/report_upload` (kept out of the regular 5s sync payload; CSV content isn't cheap
  enough to resend every tick the way settings fields are — event-driven, called once right after
  local generation, and once per `reports_needed` entry).
- **Downloading a report IS the cleanup action** — no separate delete endpoint. Render's
  `GET /api/bar/{id}/reports/{filename}` removes it from the relay's mirror immediately on success
  and queues `{"type": "delete_report", "filename": ...}` for the host to remove its own local copy
  on the next sync. User's explicit framing: "after a client admin downloaded the report, the
  system needs to be cleaned."
- Report endpoints gated by `_require_admin_token` (not the looser `_require_bartender_token`) —
  stricter than the three payment toggles bartender.html can already touch, since these are
  financial/accounting records.
- Render's pre-existing "Download CSV" button (`downloadCSV()`, a live client-side export built
  from whatever's currently in `bar_history()`) is architecturally unrelated and untouched — kept
  as its own always-fresh live view, distinct from the new file-mirror system. No wipe/dedup
  concept applies to it since it was never a stored file to begin with.

All three build-verified (`py_compile`, `xcodebuild`, `./gradlew :app:compileDebugKotlin`) and the
full wire contract spot-checked directly against the actual diffs on both platforms (not just
trusted from agent reports) — field names, endpoint shapes, and the no-op gate's exact behavior
all confirmed to match.

**Follow-up, same day: "can the kiosk get a report onto its own device's file system" — resolved**
**with no code change.** Considered making reports land in a genuinely public location (Android:
`MediaStore.Downloads`, safe and straightforward; iOS: exposing the reports folder via
`UIFileSharingEnabled` in the Files app) — but iOS has no way to expose just one subfolder of
Documents without exposing the whole thing, and Documents is exactly where `config.json` (Stripe
secret key, admin PIN hash) and all session/request/bartender data already live. Doing this safely
on iOS would have meant relocating everything else out of Documents into Application Support
first — real, security-sensitive scope, not a small addition. Turned out to be unnecessary: the
user's actual underlying goal was "get reports off the host to somewhere people can consolidate
them" — and both platforms' kiosk Reports sections already have this via their existing Share
buttons (Android's `Intent.ACTION_SEND` chooser; iOS's `ShareLink`) — email, Drive, AirDrop,
Messages, Save-to-Files, whatever's installed. No redundant second export path added.

**Spotify never silently skips a song anymore (2026-08-08), Android only — settled after three**
**rounds of design on the same day, landing on one unified mechanism.** Backlog item 12 originally
flagged one gap (the per-song "no device" cooldown-skip below the old 3-consecutive-failure breaker
threshold). The design went through real back-and-forth the same day — worth recording the
progression since the final shape isn't obvious from just the end state:

1. **First pass**: treat the two failure surfaces — `playSpotify()`'s startup failure (trying to
   *start* a song) vs. `pollSpotifyEnd()`'s mid-song failure (a song that already started going
   wrong) — completely differently. Startup trips the outage-recovery screen immediately on the
   first failure; mid-song never trips at all, just marks the request `UNFULFILLED` and skips.
2. **User caught a real problem with that split**: the *old* pre-this-session behavior (before any
   of today's changes) was exactly "keep skipping Spotify songs and playing local ones instead,
   indefinitely" during a genuine disconnect — and the user was explicit that this must never
   happen again in any form: **"in no case we should do that."** A purely "mid-song never trips"
   rule would silently recreate that exact failure mode if a disconnect happened to manifest
   through the mid-song path instead of the startup path.
3. **Final, unified design** (what's actually implemented): one mechanism for *both* surfaces, via a
   new `consecutiveSpotifyFailures` counter in `PlaybackCoordinator` and a shared
   `handleSpotifyFailure(song)` function called from all 5 failure-exhaustion points (1 in
   `playSpotify()`, 4 in `pollSpotifyEnd()`):
   - **1st consecutive Spotify failure** (any type, any path, any payment status): if the song
     belongs to a currently **paid** request (`requestedSongPaid[trackId] == true` — narrower than
     the first pass's `requestedSongIds.contains()`, since "we only need to mark PAID requests as
     unfulfilled - we don't care about FREE requests"), mark it `RequestStatus.UNFULFILLED` and
     pull that request's other not-yet-played songs off Up Next via the existing
     `cancelRequestedSongs()` (reused from the bartender Cancel-button feature, not reimplemented).
     Free/filler failures aren't marked or removed. Either way: `advance()`, keep playing.
   - **2nd consecutive failure**: instead of advancing again, call `tripSpotifyOutage()` — full
     stop, same PIN-gated recovery screen as before. The counter deliberately counts *every*
     Spotify failure toward this threshold, not just paid ones ("count any Spotify failure, not
     just paid ones" — user's explicit call after I flagged that a paid-only counter would let a
     bar running only free/filler Spotify songs never escalate during a real outage).
   - Resets to 0 on any Spotify song actually playing successfully (both success branches in
     `playSpotify()`, the healthy end-of-track branch in `pollSpotifyEnd()`, and a fresh
     `buildQueue()`) — **deliberately NOT reset inside `reconnectSpotify()`**: if a reconnect
     attempt doesn't actually fix anything, the very next failure should re-trip immediately
     rather than requiring two more strikes first.

`PlaybackCoordinator` still has zero direct reference to `LocalRequestManager` — `onSongUnfulfilled`
is now typed `(songId: String) -> Set<String>?` (returns the matched request's full `songIds`, not
just `Unit`) so `handleSpotifyFailure()` can feed them straight into `cancelRequestedSongs()`;
`markUnfulfilled()`'s return type changed from `Boolean` to `LocalRequest?` to supply this. Unlike
`markPlayed()` (last-song-only), `markUnfulfilled()` matches **any** song in a multi-song request —
the failing song could be at any position, and this data model still only has one status per whole
request (same limitation already accepted for item 5).

`UNFULFILLED` joins `PLAYED`/`DENIED` in `ReportManager`'s report-and-wipe bucket. Found along the
way: `RelayClient.kt`'s exhaustive `when` on `RequestStatus` (building the host→relay sync payload)
had no `UNFULFILLED` branch — Kotlin's compiler caught it as a build error, not a silent runtime
gap. Relay itself needed no logic changes for any of this (status is stored/forwarded as a raw
string, no server-side validation) beyond what was already built for the outage-broadcast wire
protocol (`spotify_outage_active`, Play disabled on all three surfaces while it's active) — that
part of the design didn't change across any of the three rounds, only *when* it gets triggered did.
**Kiosk-native `AdminScreen.kt` has zero request-status display of any kind, deliberately out of
scope** — LAN and render admin.html are the intended bartender-facing surfaces.

Both repos build-verified across all three rounds, and the final consolidated implementation
spot-checked directly against the diffs (all 5 `handleSpotifyFailure()` call sites, the 4 reset
points, the `onSongUnfulfilled`/`markUnfulfilled()` signature change) — not just trusted from the
forks' reports.

**Exhaustion-reshuffle no longer scatters not-yet-played requests (fixed 2026-08-08, item 13, Android
only)** — `PlaybackCoordinator.advance()`'s exhaustion branch (fires when the queue wraps at the end of a
full pass) used to do a flat `queue = queue.shuffled()` over the whole list, with no distinction between
ordinary filler songs and a customer-requested song still sitting unplayed in Up Next. A request could
land anywhere in the reshuffled order, worst case delayed by up to a full catalog length before its turn
came back around. (The existing "resurrection fix" in `RelayService.kt` only prunes a song from
`requestedSongIds` once it's actually played — it never addressed *position* for a request that hadn't
played yet.) Fixed by partitioning the queue at reshuffle time: `val (upNextLeftover, filler) =
queue.partition { it.id in requestedSongIds }`, then `queue = upNextLeftover + filler.shuffled()` — Up
Next leftovers stay exactly where they were, in their existing relative order, completely untouched;
only the filler portion gets freshly shuffled. Deliberately **no deduplication** between the two groups —
an earlier design draft considered removing an Up-Next song from the first N positions of the fresh
shuffle if it also appeared there (to avoid a played-then-immediately-replayed near-term repeat), first
with N = Up Next size, then reconsidered as a larger constant window (10-15) once the dynamic N was
found not to fully prevent near-term repeats — but a constant window needs a recursive "reshuffle until
clean" loop to actually guarantee no near-term duplicate, which risks a starvation bug on a small
playlist (everything eligible could get excluded, leaving nothing to select). Rejected as not worth the
complexity for what's ultimately a minor cosmetic imperfection — a requested song coincidentally landing
again soon after in the freshly-shuffled filler is accepted as ordinary shuffle luck, not something worth
engineering around. iOS was not touched — `MPMusicPlayerController.applicationQueuePlayer`'s shuffle is
native/opaque, no equivalent flat-list reshuffle exists to fix. This fix does not change how
`injectSongs()` places a newly-approved request added mid-shuffle — it still lands near-term exactly as
before.

## Planned next
- Song counts from iOS/Android on register: `artists: [{name, song_count}]` instead of `[String]` — improves pie chart accuracy
- Stripe live key: apply under own business account to validate payment flow end-to-end before bar rollout
- Apple Pay domain file is served; needs Stripe dashboard domain registration to activate
- LLM fallback for obscure artists (no Last.fm data): deferred — needs API cost/rate infrastructure first
- Recommended playlist pipeline: deferred (see `docs/render_spec.md`)
