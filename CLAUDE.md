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
Android** — at the time this shipped (2026-08-01), a deliberate departure from Android's admin-PIN
comparison, which was still plaintext (`BarDetails.pin`, pre-existing inconsistency vs iOS/relay,
not in scope for this pass). **That inconsistency was closed on 2026-08-08** — see the entry further
below ("Two 'deliberately out of scope' gaps closed") — Android's admin PIN is now hashed
(`BarDetails.pinHash`) the same way. The bartender field had no existing plaintext-comparison
precedent to preserve, and a hash keeps LAN
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
list/kill sessions. **LAN gained the identical separation on 2026-08-09** (was previously a known,
accepted scope boundary — see the entry below, "LAN bartender-credential exposure closed," for why
it turned out cheaper than originally assessed) — `bartenderId`/`BartenderRecord.id` remains the
actual bearer token used for approve/deny/list, but the Sessions tab's list/kill endpoints now go
through a separate opaque `sessionId`, mirroring the relay exactly.

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

**Two "deliberately out of scope" gaps closed, Android + iOS only, no relay involvement**
**(2026-08-08).** Reviewing the standing list of scope boundaries this session had accumulated
(LAN's unauthenticated player/report endpoints, Android's plaintext admin PIN, LAN's exposed
bartender credential, MDM's hard platform ceiling), the user asked to close the first two —
cheap, real hardening — and leave the latter two as-is (LAN credential separation is a genuine
refactor; MDM isn't fixable from app code at all). **Update, 2026-08-09: the third item (LAN's**
**exposed bartender credential) turned out cheaper than assessed and was also closed** — see its
own entry further below. **The fourth (MDM) was removed from the tracked list entirely the same
day** — it was never a deferred-but-doable item, just a fact about what app code can and can't
achieve; see the "Kiosk lock-to-app" entry above for the full explanation, which stands
permanently and isn't expected to change.

1. **LAN playback control and report endpoints had zero auth gate.** `/api/player/{play,pause,
   next,prev}` and all of `/api/reports*` (list/generate/download/delete) on both platforms'
   `LocalServer` took no token at all — confirmed by direct code read, not assumption. Anyone on
   the bar's wifi/hotspot, no PIN needed, could pause/skip the live show or pull/delete session
   report CSVs. Both only ever get called from admin.html (bartender.html has no player section
   and never touches reports on either platform, confirmed via grep) — gated with
   `isValidAdminToken` specifically (not the looser `isValidActionToken`/`isValidSettingsToken`),
   same admin-only reasoning already applied to the relay's own report endpoints and the
   Bartender Sessions tab (financial records, least-privilege). Android: routing in
   `LocalServer.kt` now threads `session` through to `playerCmd()`/`handleListReports()`/
   `handleGenerateReport()`/`handleDownloadReport()`/`handleDeleteReport()`, each checking
   `requestManager.isValidAdminToken(session.parms["token"] ?: "")` before doing anything. iOS:
   the same 6 `LocalServer.swift` closures gained `[weak self]` + `self.isValidAdminToken(req.
   queryDict["token"])`, with `/api/player/*`'s pre-existing `checkSession`/`sessionExpired()`
   check kept as a separate, prior guard (a different, general "is this a live LAN session at
   all" check, not actor authorization — the two are complementary, not redundant). Both
   `assets/admin.html` and `WebApps/admin.html` updated identically to append
   `&token=`/`?token=` (already-cached `barConfig.token` from the existing PIN-auth flow) to
   every one of these fetch calls.

2. **Android's admin PIN was compared in plaintext** (`BarDetails.pin`), inconsistent with the
   bartender PIN (SHA-256, added earlier this session) and with iOS/relay, both of which already
   hash the admin PIN (iOS: `BarConfig.pinHash`, hashed on-device via `CryptoKit` at every
   compare/save site — confirmed by reading `AdminView.swift`/`SetupView.swift`/`LocalServer.swift`
   directly, used as the exact template for Android's fix). Renamed `BarDetails.pin` →
   `BarDetails.pinHash`; every write site now hashes (`SetupWizardScreen.kt`'s wizard PIN step,
   `KioskView.kt`'s on-device Forgot-PIN reset flow) and every compare site hashes-then-compares
   (`KioskView.kt`'s on-device `AdminPinEntry.verify()`, `LocalRequestManager.checkAdminPin()` for
   LAN auth, called from `LocalServer.kt`'s `handleAdminAuth()`) — mirrors the exact idiom
   `LocalRequestManager.pairBartender()` already used for the bartender field (hash internally,
   caller always passes the raw value). `RelayClient.kt`'s register/sync payload used to compute
   `sha256(barDetails.pin)` client-side on every call — now sends `barDetails.pinHash` directly,
   since it's already hashed (leaving the old `sha256()` helper in place would have double-hashed
   silently, desyncing the relay's copy from what LAN/kiosk accept — caught and fixed before it
   shipped, not after).
   - **On-device storage migration, not a breaking change**: existing installs have a plaintext
     PIN already saved in `SharedPreferences` under `bar_pin`. `MainActivity.kt`'s restore-state
     read now migrates it in place, once: a real SHA-256 hex digest is always 64 characters, a
     stored plaintext 4-6 digit PIN never is, so a value that isn't 64 chars gets hashed and
     immediately re-persisted under the same key. No forced Forgot-PIN recovery on update.
   - **Real, deliberate UX side effect**: the wizard's PIN step (`AdminPinStep.kt`) used to
     prefill both PIN fields with the actual current plaintext PIN when re-running setup on an
     existing bar — convenient (just tap Next to keep it unchanged) but only possible because the
     value was plaintext. With only a hash on hand, there's nothing meaningful left to prefill
     with. Changed to start blank always; leaving it blank and tapping Next still means "keep my
     current PIN" (unchanged `hasSavedPin`-driven behavior, just without ever displaying the old
     PIN on screen) — arguably a small security improvement in its own right (the PIN no longer
     flashes on screen just from re-entering the wizard), and matches iOS's `SetupView.swift`,
     which was already built this way from the start (never prefilled, since it was always
     hash-only on that platform).

   Both platforms build-verified (`./gradlew :app:compileDebugKotlin`, `xcodebuild ... build`).
   No relay changes for either fix — purely host-app hardening. Not committed as of this
   writing in this pass; per the standing [[feedback_relay_push_policy]] policy, ship together
   with jukebarweb once this doc update lands.

**LAN bartender-credential exposure closed (2026-08-09), Android + iOS only, no relay involvement**
**— the third "deliberately out of scope" item from the previous day, revisited and fixed.** The
concern (see the entry above): `GET /api/bartender/sessions` had to return each paired bartender's
real identifier so admin.html could offer per-row Kill — but that identifier doubled as the actual
bearer token checked on every approve/deny/list call, so the response (sent over plain LAN HTTP)
carried a working credential for every currently-paired bartender, not just a display id. Originally
assessed as a real refactor and left alone alongside the render fixes the day before. On revisiting
with the user (prompted by walking through the actual exploit path — passive LAN sniffing gated on
the admin visiting the Sessions tab at some point during the capture window, with no way for the
admin to detect after the fact that a token had been skimmed, unlike PIN brute-forcing which shows
up as failed-attempt counters), the fix turned out much smaller than the original assessment: **it
doesn't require splitting identity from credential everywhere** — bartenders still authenticate
with `bartenderId`/`BartenderRecord.id` exactly as before, unchanged, everywhere except the Sessions
tab's own two endpoints. Added one new field, `sessionId` (opaque, minted alongside the real id at
pairing time, mirrors the relay's identical `session_id`/token split in `bar_authenticate()`):
`GET /api/bartender/sessions` now returns `session_id` instead of the real id; `POST .../kill` now
accepts `session_id` and looks up the matching bartender internally before deleting by its real id.
The admin's browser — and anything sniffing the LAN traffic in transit — now only ever sees an id
that's useless for authenticating as that bartender.

**iOS needed a migration, Android didn't** — Android's bartender records are in-memory only
(`CopyOnWriteArrayList`, cleared on process restart), so every instance goes through the current
constructor and always has a `sessionId` from the moment the updated build runs; no existing state
to reconcile. iOS's `BartenderRecord` is disk-persisted (one JSON file per bartender via
`LocalStorage`), so a bartender paired before this update has no `session_id` key in their saved
file. Added `sessionId: String?` (Optional, mirroring the exact precedent this same struct already
set for `ip: String?` — "records persisted before this field existed still decode fine") and a
self-healing migration at the point of first read: `GET /api/bartender/sessions`'s handler assigns
and persists a fresh `sessionId` for any record still missing one, the same lazy-migrate-on-read
shape as the Android admin-PIN-hash migration from the day before.

Both platforms build-verified (`./gradlew :app:compileDebugKotlin`, `xcodebuild ... build`) and the
diffs spot-checked directly — confirmed the pairing/approve/deny/list code paths that authenticate
*as* a bartender were untouched, only the two Sessions-tab endpoints and their `assets/admin.html`/
`WebApps/admin.html` call sites changed. No relay changes — this was purely a LAN-only asymmetry
the relay never had in the first place.

**Novice/external beta-tester guide added (2026-08-16), `docs/beta_tester_guide.md`.** User needs
12 outside, non-developer testers to run through the request-flow scenarios — `docs/full_system_test_plan.md`'s
section A (9 end-to-end cross-surface scenarios) is written for developers and leans on internal
names throughout (`AppState.swift`, `effective_stripe`, `desired_settings`, etc.), which isn't
usable by someone with no codebase knowledge. Rather than multiple small per-case files (considered
and explicitly rejected — user wants one file people can be handed a section of, not a folder to
navigate), this is one document with 9 sections, each covering the same ground as `docs/full_system_test_plan.md`'s
A1–A9 but **fully self-contained and deliberately repetitive** — a tester assigned one section
should never need to read another section or scroll elsewhere for context, so setup steps are
restated in full every time rather than cross-referenced. Every section follows the same shape:
What You'll Need → Setup → What To Do → Success Looks Like → Something's Wrong If.

**UI copy was verified against the actual code, not invented** — every button/toggle/label name
quoted in the guide ("Stripe 💳", "Pay to bartender", "Accepting requests", "Local Only" / "Local +
Remote" / "Remote Only", "Use Internet Mode", "Enter the admin PIN to continue", "Request", "Please
ask staff for assistance", "Re-attach to Spotify", "Cancel", etc.) was grepped directly from
`static/admin.html`/`static/customer.html` (platform-shared, so exact wording is trustworthy for
any host) and Android's setup-wizard/kiosk Compose files. Where iOS's exact wizard copy wasn't
independently verified with the same confidence, instructions were phrased descriptively rather
than risk quoting wrong text to an iOS tester (e.g., "the screen where you choose how customers
should connect" instead of assuming an exact iOS button label matches Android's).

**Setup-flow accuracy, worth remembering**: kiosk display mode (Local Only / Local + Remote /
Remote Only) has no live in-session toggle — changing it requires End Session + redoing setup from
scratch (same fact already documented earlier in this file). The guide's kiosk-mode-switching steps
(test cases 6 and 8) were written this way from the start after cross-checking against that
existing note, not assumed.

**Android's "Local Only" setup-wizard copy was factually wrong, found and fixed 2026-08-16,**
**Android only.** Surfaced while writing the beta tester guide above — comparing the wizard's three
display-mode cards against iOS's found Remote Only and Local + Remote already at good wording
parity, but Android's Local Only card claimed *"Requests are auto-accepted"* (badge) and *"Requests
are auto-accepted — no QR code"* (detail). That's wrong: display mode and payment/approval mode are
independent choices (see `KioskDisplayMode` vs. payment-mode notes elsewhere in this file) — a bar
in Local Only can still pick "Pay to bartender" on the very next wizard screen, which requires
approval same as any other mode. iOS's copy already got this right (*"free vs. pay-to-bartender is
still your choice on the next screen"*) and additionally clarified that admin/bartender stay
reachable over WiFi/Hotspot/Internet regardless of display mode, a point Android's copy omitted
entirely. Fixed `DisplayModeStep.kt`'s Local Only card to match iOS's accurate framing verbatim for
the first two sentences, keeping Android's own correct Screen Pinning recommendation (vs. iOS's
Guided Access one) as the platform-specific third sentence — same pattern the Local + Remote card
already used successfully. Build-verified (`./gradlew :app:compileDebugKotlin`). This was a real
operator-facing bug, not cosmetic verbosity — a bar owner reading the old copy could reasonably
have believed Local Only forced auto-accept and picked "Pay to bartender" thinking it wouldn't
actually apply.

**Android's Bar WiFi setup card duplicated the detected network name in a redundant, editable**
**field — found and fixed 2026-08-16.** User noticed the WiFi step showed the joined network in
green (correct, matches iOS) but Android *also* showed a separate, editable "WiFi network name"
text field pre-filled with the same value — implying the two might legitimately diverge, when
they shouldn't. iOS's equivalent card (`NetworkGateView.swift`'s `wifiCard`) never shows an
editable field at all, only the green detected-network badge plus a password field. Checked why
Android has the field before just deleting it to match: `wifiStatus()`'s own comment explains SSID
detection genuinely can fail on Android without `ACCESS_WIFI_STATE`/location permission (iOS
doesn't have this failure mode) — so the field isn't pure duplication, it's an undifferentiated
fallback shown unconditionally instead of only when actually needed. Fixed
`NetworkModeStep.kt` to compute `ssidDetectionFailed = wifiConnected && detectedSsid == null` and
only render the editable name field in that case — when detection succeeds (the common case),
behavior now matches iOS exactly: green badge only, no redundant/editable duplicate. Android-only,
no iOS or relay involvement (iOS was already correct). Build-verified.

**Kiosk-native Admin screen brought to full parity between platforms (2026-08-16), both**
**`AdminScreen.kt` and `AdminView.swift` — content/order/labels, not a visual redesign.** User
audited the two screens side by side (this doubles as the wizard's SUMMARY step, since both
platforms reuse the same live-admin composable there) and found several real divergences:

1. **Section order didn't match, and neither matched the remote admin pages.** Payments, Requests,
   and Bartender Access were scattered after Session/Setup on both platforms — nowhere near each
   other. `static/admin.html`'s Actions tab already groups these three back-to-back (Payments →
   Requests → Bartender Access). Both kiosk screens now use that same order, moved up to sit right
   after the player-controls card, with Session and Setup pushed down below them.
2. **iOS never showed kiosk display mode (Local Only / Local + Remote / Remote Only) anywhere on
   the admin screen at all** — its Setup section had a row simply labeled "Mode," which is
   actually *network* mode (wifi/hotspot/internet), not kiosk mode. This ambiguity is exactly what
   led to the confusion in the first place — the user's own description of "where iOS shows kiosk
   mode" pointed at this mislabeled row. Fixed two ways: renamed to "Network mode" (matching
   Android's existing correct label) and added a genuinely new "Kiosk mode" row to the Session
   section on iOS, matching Android's (which already had it, just needed to move — see above).
3. **"For three" (iOS) vs "Bundle (3 songs)" (Android)** — unified on Android's clearer wording.
4. **Session ID / Jukebar ID question — kept on both, deliberately not prominent.** These aren't
   operationally useful to a bar owner day to day, but they're the one lightweight way to trace a
   specific device/session against relay-side records if something needs debugging (e.g. during
   beta testing with external testers who can't read logs themselves) — worth keeping for that
   reason alone, at the low cost of two de-emphasized rows. Android didn't show them before; both
   platforms now do, in the same truncated (`.take(8)`) style already used for other identifiers
   on this screen.
5. **Bartender Access status text didn't explain cause and effect.** Old copy ("no bartender QR
   code or bartender page exists...") stated the *symptom* without saying *why*. Reworded on both
   platforms to state the cause explicitly: "Off — no bartender PIN has been set, so no bartenders
   can register or use the bartender page." / "On — a bartender PIN is set, so the bartender QR
   code and page are active."
6. **Android's "Generate Report Now" button was at the bottom of the Reports section, styled as an**
   **outlined/ghost button** — unlike iOS, which already had it as the section's first row. Moved
   to the top on Android (list of past reports now follows it, matching iOS's order) and restyled
   to the same filled Coral button used for every other primary action on this screen (Save, Turn
   Off, End Session) instead of the one-off outline style it had.

Section title wording was also aligned where it had silently drifted (iOS's "Pricing & Payments" →
"💳  Payments", "Bartender Access" → "🍸  Bartender Access", matching Android's emoji-prefixed
titles exactly). Both platforms build-verified (`./gradlew :app:compileDebugKotlin`,
`xcodebuild ... build`) and the full diffs spot-checked directly — confirmed final section order
matches exactly (`grep`'d both files' card/section titles top to bottom) and that no functional
logic (toggle handlers, report generation, QR generation) changed, only presentation/order/copy.

**Admin screen parity follow-up (2026-08-16), both platforms — four more fixes found in the same**
**audit pass as the section above.**

1. **"Approve/deny... happen here in Admin" was factually wrong on the kiosk-native screens
   specifically.** `AdminScreen.kt`/`AdminView.swift` have zero request-approval UI of their own
   (confirmed elsewhere in this file: "Kiosk-native `AdminScreen.kt` has zero request-status
   display of any kind... LAN and render admin.html are the intended bartender-facing surfaces")
   — so when Bartender Access is off, approve/deny does NOT happen "here" on this exact screen,
   it happens on whichever Admin page (LAN or render) is actually connected to the kiosk. Fixed on
   both platforms: "Approve/deny/settings happen on the Admin pages connected to this kiosk
   instead." `static/admin.html`'s identical-looking copy was deliberately left unchanged — "here
   in Admin" is accurate there, since that page genuinely is where the action happens.
2. **iOS never showed the bar's own name anywhere on the admin screen — not in Session, not in the
   header, nowhere.** Android's header already does (logo + "JukeBar" + bar name + "· Admin," a
   custom Compose Row). Added the same content to iOS via a `.principal` toolbar item (SwiftUI's
   `NavigationStack`/`Form` combo doesn't support a rich custom title the way Android's plain
   `Column` header does, so this is the closest equivalent — reused the exact same
   `GrapefruitLogoView`/`RainbowText` views the app's own setup-wizard screens already use for
   their headers, not new components) — `.navigationTitle("Admin")` kept alongside it purely for
   VoiceOver, since the principal view is what actually renders.
3. **Once the bar name is in the header on both platforms, the "Bar" row in the Session section is
   redundant** — removed from Android (which had it in both places); iOS never had it duplicated
   to begin with, so no change needed there beyond not adding it.
4. **Android's Refresh Catalog button was a one-off tonal/light-Coral style** — every other
   primary action on this screen (Save, Turn Off, End Session, Generate Report Now) uses solid
   filled Coral with black text. Unified to match; iOS's equivalent buttons were already
   self-consistent (plain default Form-row style throughout) and didn't need a change.

Both platforms build-verified. No relay changes.

**Stripe publishable-key display fixed on both platforms (2026-08-16) — the displayed key looked**
**like it "didn't match" what was typed, but the underlying data pipeline was fine.** User reported
the key shown on Android didn't match what was set on the pricing wizard step. Traced the actual
data flow (`PricingStep.kt`'s pre-filled/editable `stripePublishableKey` state → `onFinish` →
`SetupWizardScreen.kt`'s `barDetails.copy(stripePublishableKey = stripePk, ...)` → `AdminScreen`'s
`barDetails` param) before touching anything — no structural bug found there. The real cause:
Android's display only ever showed the **last 6 characters** (`"pk_…${key.takeLast(6)}"`), which
is close to impossible to visually cross-check against the full key on the previous screen. iOS
showed more (first 14 chars) but still truncated, plus a separate "Secret key: ✓ Stored" row that
added noise without telling the operator anything actionable. Since a *publishable* key is
explicitly safe to expose — that's the entire reason Stripe calls it that, distinct from the
secret key. Both platforms briefly showed the full, untruncated key as a plain caption under the
Stripe toggle — **corrected same day**: the full key doesn't fit on a narrower device at this font
size, wrapping/truncating unpredictably (this is what actually produced the "pk_ … end-of-key"
layout the user then noticed on Android). Settled on first 15 characters + "…" on both platforms
(`key.take(15)` / `pk.prefix(15)`, identical truncation point) — long enough to visually confirm
against the pricing step's value (distinguishing test/live and catching a wrong paste), short
enough to always fit on one line. The "Secret key: Stored" row is still gone from iOS entirely
(Android never had one). iOS kept its "⚠ No publishable key" warning (a genuinely actionable
message, unlike the routine "stored" status) and `.textSelection(.enabled)` so the truncated key
can still be long-pressed to copy/compare. Both platforms build-verified. No relay changes.

**Kiosk lock-to-app, item 14 follow-up (2026-08-16), Android only: re-lock after an unpin,**
**without restarting the app or session. — SUPERSEDED 2026-08-17, this entire approach was**
**removed; see the "polling-based unpin-detection layer... was removed entirely" entry further**
**below for why and what replaced it.** Kept here for the historical record of how the design got
there, not as a description of current code. User noticed the standard Android unpin gesture (hold
Back+Overview, or swipe-up-hold on gesture nav) works for anyone, not just staff — the original
item 14 design already documented this as the ceiling of what `startLockTask()`-only pinning can
do without Device Owner provisioning, but hadn't built a mitigation. Two real Android platform
facts surfaced while building this, both confirmed directly rather than assumed after the first
one turned out wrong:

- **`Activity.onLockTaskModeChanged(Int)` does not exist.** Checked directly against the compileSdk
  36 stubs via `javap` — `Activity` only exposes `startLockTask()`/`stopLockTask()`/
  `showLockTaskEscapeMessage()`, no mode-changed callback at all. (This was a wrong recollection on
  my part — worth remembering not to assume a plausible-sounding Android API exists without
  checking, same lesson as elsewhere in this file about not trusting memory over the current
  state of things.)
- **`DevicePolicyManager.ACTION_LOCK_TASK_ENTERING`/`ACTION_LOCK_TASK_EXITING` exist but are**
  **`@SystemApi`-restricted** — present in the stub jar's bytecode (hence visible to `javap`) but
  genuinely unusable from a normal third-party app; referencing either produces a real "Unresolved
  reference" compile error, not just a runtime permission issue.

With no callback or broadcast available, the actual mechanism is **polling**
`ActivityManager.lockTaskModeState` once a second inside the existing pinning `LaunchedEffect`
(`MainActivity.kt`) — at a 1s interval this is indistinguishable from a true callback in practice,
since whoever did the unpin gesture is bounced into a full-screen block (`KioskUnpinnedScreen`,
new in `KioskView.kt`, styled identically to the existing `SpotifyOutageScreen` block-and-recover
pattern) well before they could realistically reach the home screen or another app. Re-entering
the admin PIN there calls `updateLockTaskPinning(true)` to re-pin immediately — genuinely no app
or session restart needed, which was the second half of the ask. Made `kioskUnpinned` itself a key
on the `LaunchedEffect` (alongside the existing `isSetupComplete`/`kioskMode` keys) specifically so
clearing it (via a correct PIN) restarts the effect and resumes polling for the *next* unpin — the
first draft only polled once per session and would have silently stopped protecting the kiosk
after the first recovery. iOS is untouched — same hard platform asymmetry as the rest of item 14
(no programmatic Guided Access trigger or equivalent state-change signal exists on that platform
at all, so there's nothing to build there). Build-verified
(`./gradlew :app:compileDebugKotlin`). No relay changes.

**Kiosk re-lock: false-positive fixed + device lock-screen advisory added (2026-08-16, same day**
**as the feature above), Android only.** **The false-positive fix described here turned out not to**
**be the actual fix — the same bug recurred the next day and this whole mechanism was removed; see
below.** The device lock-screen advisory text added in the same pass is unaffected and still
current. User caught a real regression live, minutes after the
re-lock feature shipped: after finishing the wizard and tapping "Launch Kiosk," the device
genuinely pinned (OS confirmed it), but the app flipped into `KioskUnpinnedScreen` about a second
later anyway — a false positive, not a real unpin. Root cause: the polling loop's first check ran
exactly at the 1s mark, and `startLockTask()` doesn't take effect instantly — the very first time
a device ever pins, Android shows a one-time "Screen pinned" system tutorial that can push the
actual `lockTaskModeState` transition out past a single 1-second check. Fixed by splitting the
effect into two phases: a warm-up loop (up to 5s, checking every 500ms) that waits for pinning to
be genuinely confirmed before ever starting to watch for an unpin, followed by the steady-state
1s watch loop from before. If warm-up never confirms pinning at all (OEM restriction, etc.), the
effect exits quietly rather than showing a false unpin prompt for something that was never pinned
— `updateLockTaskPinning()` already logs that failure separately.

Same message also raised a second, distinct point: even with pinning working perfectly, the kiosk
keeps the screen on indefinitely (`FLAG_KEEP_SCREEN_ON`, unrelated to lock-task mode) — but a
bystander can still manually sleep the screen with the physical power button, which
`FLAG_KEEP_SCREEN_ON` does nothing to prevent. Without a device-level lock-screen credential,
waking it back up lands directly back in the (still-pinned) kiosk with no gate in between; with
one, waking requires passing the OS's own lock screen first — a real, independent security
boundary neither `startLockTask()` nor this session's app-level PIN-recovery screen can provide on
their own. Added a matching recommendation to the same wizard advisory that already recommends
enabling Screen Pinning (`DisplayModeStep.kt`'s Local Only / Local + Remote cards) — same
placement, same "why" framing. iOS not touched for this specific point: Guided Access, iOS's
equivalent lockdown mechanism, already has its own built-in exit-passcode gate as an inherent part
of turning it on, so the same underlying concern is already covered there once an operator follows
the existing Guided Access advisory — no parallel change needed. Build-verified.

**Device Protection: force-lock on exit, in every kiosk mode (2026-08-16), Android only.** User
clarified the actual threat model behind the device lock-screen advisory above: not "someone puts
the screen to sleep," but "someone exits the pinned app — by any means, unpinning or otherwise —
and then has unsupervised access to the device's home screen and Settings." The app-level PIN
screen from earlier the same day doesn't solve this: it can only ever show while the app is in the
foreground, so if someone leaves without ever returning to it, that screen never appears at all.
Explicitly requested this apply **even in `remoteOnly` mode** — unauthorized physical access is a
concern regardless of whether customers were ever meant to touch the screen.

The real mechanism is `DevicePolicyManager.lockNow()`, called from `Activity.onStop()` — which
fires the instant the Activity leaves the foreground for *any* reason (unpinning, Home, recent-
apps switch), unlike the Screen Pinning watch loop above which only reacts to a specific gesture.
`lockNow()` requires the app to be an active **Device Administrator** — the lightweight, standard
Android permission grantable via a one-time system consent dialog
(`Settings.ACTION_ADD_DEVICE_ADMIN`), explicitly not Device Owner/MDM provisioning, which stays
out of scope everywhere else in this feature. New `KioskDeviceAdminReceiver` (a minimal
`DeviceAdminReceiver`, only declares the `force-lock` policy — no password-quality control, no
remote wipe, nothing else requested) registered in the manifest; `onStop()` calls `lockNow()`
whenever the kiosk is live (`isSetupComplete`, no kiosk-mode filter at all — deliberately broader
than Screen Pinning's scope) and the admin is confirmed active.

New "🔒 Device Protection" card in `AdminScreen.kt` (placed right after Bartender Access, in the
same security-cluster group moved up earlier this session) shows current status and an
Enable/Turn Off button. Also checks `KeyguardManager.isDeviceSecure()` and shows a warning if the
device has no lock-screen credential of its own — `lockNow()` does nothing meaningful without one,
same dependency as the wizard advisory added earlier the same day. No direct callback exists for
"the system consent screen just closed," so status re-checks on every `ON_RESUME` via a
`LifecycleEventObserver` — standard pattern for a permission/settings screen with no result
callback, first use of this specific idiom in the codebase.

**Caught and fixed one real XML gotcha before it shipped**: both the manifest comment and the new
`device_admin_policies.xml`'s comment originally used `--` as a plain-prose em-dash substitute
(this session's own writing convention elsewhere) — but a literal `--` inside an XML comment is
invalid and fails resource/manifest parsing outright. `./gradlew :app:compileDebugKotlin` alone
didn't catch this (Kotlin compiles fine regardless of XML validity); `:app:assembleDebug` did,
which is why that was run as the final check here rather than stopping at the Kotlin-only build
this session otherwise defaults to. Fixed by using a real em dash character instead. No relay or
iOS changes — iOS has no equivalent Device Administrator concept to build against, consistent with
the rest of item 14's platform asymmetry.

**The polling-based unpin-detection layer (`KioskUnpinnedScreen`) was removed entirely (2026-08-17),**
**one day after shipping, in favor of Device Protection alone.** It misfired live in testing right
after it shipped (a false "unpinned" trip on every kiosk launch), got a real fix attempt the same
day (a warm-up phase before the watch loop starts), and **still misfired after that fix** — caught
by the user on a fresh rebuild the next day. Rather than chase a third timing bug in a
poll-against-`lockTaskModeState` design, removed it outright: `MainActivity.kt`'s `LaunchedEffect`
is back to the original one-line `updateLockTaskPinning(shouldPin)` from before any of this, no
`kioskUnpinned` state, no watch loop; `KioskView.kt`'s `KioskUnpinnedScreen` and its
`kioskUnpinned`/`onRePin` parameters are gone. **Screen Pinning itself
(`startLockTask()`/`stopLockTask()`) is untouched** — still active for `localOnly`/
`localAndRemote`, still a real deterrent; only the "detect an unpin and show a PIN-recovery
screen" layer built on top of it the day before is gone.

This wasn't just abandoning a broken feature — the user's own observation made it clearly the
right call, not just the path of least resistance: Device Protection's `onStop()` → `lockNow()` is
strictly better for this exact job. It's a plain Activity lifecycle callback (`onStop()` fires
reliably and promptly whenever the Activity leaves the foreground, for any reason, guaranteed by
the platform) rather than a poll racing against `ActivityManager.lockTaskModeState` on a timer —
no warm-up window to get wrong, no steady-state interval to misfire. It also reacts to leaving the
app *at all* (Home, recent-apps switch, the unpin gesture, anything), not just one specific
gesture, and forces the device's actual OS lock screen rather than an app-level PIN entry that
could only ever help if someone came back to this app afterward — a limitation already known and
accepted about the polling approach even before its two live failures.

**Root-caused why the user never saw the Device Admin consent dialog during the wizard, since**
**it looked like a separate bug at first but wasn't one**: "Enable Device Protection" only exists
on the live admin screen (reachable by unlocking Admin from the kiosk's now-playing view with the
admin PIN) — never something the wizard prompts for automatically, by design, same as every other
admin-screen-only setting in this app. `KioskUnpinnedScreen` misfiring on every launch meant the
user could never get far enough into a normal session to reach that screen at all, so the consent
dialog was never reached to be shown — not evidence Device Protection itself was broken, just
evidence it was unreachable. Confirmed this reasoning explicitly rather than asserting it, since a
plausible-sounding root cause is still a guess until it's traced end to end. Build-verified via the
full `:app:assembleDebug` (not just `:app:compileDebugKotlin`) after the removal, matching the
lesson from the Device Protection feature's own XML comment mistake the day before — a
Kotlin-only build doesn't validate manifest/resource changes, and this removal touched enough
surface area to be worth the fuller check again.

**Device Protection given its own wizard step (2026-08-17), Android only.** The Enable/Turn Off
control had only ever existed on the live admin screen, reachable by unlocking Admin from the
kiosk — nothing in setup ever prompted for it, so in practice it went unused (this is also root of
why the user never saw the Device Admin consent dialog during the wizard: nothing pointed there).
New `WizardStep.DEVICE_PROTECTION`, inserted right after Admin PIN — "you just secured admin
access with a PIN, now secure the device itself" reads as one continuous idea, and it's the
natural place given the step is otherwise about device-level security. New
`DeviceProtectionStep.kt` explains the actual gap in plain language before asking for anything
(the admin PIN alone doesn't stop someone from just leaving the app and reaching Settings
directly), shows live status, and reuses the same `KioskDeviceAdminReceiver`/`isAdminActive()`/
resume-triggered-recheck pattern as the existing `AdminScreen.kt` card — that card is unchanged
and still there for enabling/disabling later, the wizard step doesn't replace it.

**Deliberately still skippable, not a blocking gate** — granting Device Admin is a real system
permission with its own consent dialog; forcing it would be worse than clearly recommending it and
moving on if declined, same philosophy already established for the non-blocking Screen Pinning /
device-lock-screen advisories on the Display Mode step. The Next button's label reflects this
plainly rather than pretending it's neutral: "Next →" once enabled, "Skip for now →" while it
isn't — visible, honest friction instead of a silent bypass. Build-verified via
`:app:assembleDebug`. No relay or iOS changes — iOS has no Device Administrator equivalent to
build a matching step around.

**Screen Pinning never re-established itself after being exited (2026-08-17), Android only.**
User confirmed Device Protection works exactly as intended (screen locks immediately on exit,
requiring the device's own PIN), then asked the natural follow-up: once pinning is exited, is there
any way back into a pinned state short of tearing down the whole session? There wasn't — the
pinning `LaunchedEffect` only reacts to `isSetupComplete`/`kioskMode` *changing*, and neither
changes just from returning to the app (via the unpin gesture and back through Device Protection's
lock screen, or any other path), so pinning silently stayed off indefinitely once lost. User's own
proposed fix — re-pin when closing the kiosk's Admin panel via "Done" — was exactly right and is
now wired in (`KioskView.kt`'s `onAdminClosed` callback, invoked alongside the existing
`showAdminScreen = false`). Added a second, more general fix alongside it: `MainActivity.kt`'s
`onResume()` now also reasserts pinning on every return to the foreground, covering paths the
Admin-Done trigger can't reach — most importantly, coming back through Device Protection's own
lock screen, which is a real Activity pause/resume cycle. Both routes share one new
`reassertLockTaskPinning()` helper (guarded by the existing `updateLockTaskPinning()`'s
already-pinned no-op check, so calling it redundantly from both paths is harmless). Build-verified
via `:app:assembleDebug`. No relay or iOS changes.

**Device Protection's ~1s unpin-to-lock window is accepted as the practical ceiling (confirmed**
**2026-08-17, Android), and iOS has no equivalent capability to build at all — checked, not**
**assumed, same day.** User tested live: after unpinning, other apps/Settings are briefly reachable
(roughly a second) before the screen goes dark and asks for the device PIN. Root cause isn't a gap
in the app's logic — it's Android's own unpin-gesture animation plus `lockNow()`'s render time,
both outside the app's control. The only way to close it further would be firing from `onPause()`
instead of `onStop()`, which triggers earlier but also fires for transient interruptions (a
permission dialog, the notification shade, even Device Protection's own consent dialog) — would
falsely lock the device during ordinary use, including possibly while the operator is *trying* to
grant Device Protection in the first place. Confirmed correct to leave on `onStop()`.

**iOS**: re-checked whether an equivalent "lock the device when focus leaves the app" is possible
at all — it is not, and for a structurally different reason than Android's timing gap. Android's
`lockNow()` exists because Google exposes "force lock" as one of a small set of permitted Device
Administrator actions to any app. Apple has no public API of any kind for a third-party app to lock
the device screen — the only path is the MDM `DeviceLock` remote command, which requires Supervised
enrollment, the same ceiling item 14 already documents for Guided Access. Guided Access (existing,
manually-enabled-only, already detected and warned-about in `KioskView.swift`) remains the closest
iOS analog — arguably a stronger mitigation in one sense, since once active it blocks leaving the
app at all rather than locking a moment after. No code change; confirmed via direct source read
(`KioskView.swift:26-33`, the existing "no public API" comment) rather than re-litigating from
memory.

**Guided Access given its own wizard step (2026-08-17), iOS only — the mirror-image of Android's**
**Device Protection step, for the platform that has no programmatic equivalent at all.** Once the
prior research confirmed Apple exposes zero API for a third-party app to lock the device or
start/stop Guided Access (see the entry above this one), the user's own reaction was that Guided
Access is "EXACTLY what we need" and asked for it to get the same wizard-step treatment Android's
Device Protection just got, right after the payment-choices screen. New `SetupStep.guidedAccess`
case, inserted between `.approvalMode` (the "enable one or both payment methods" screen — the
actual "payment choices" screen the user meant, confirmed by the step's own on-screen copy) and
`.pricing`; `SetupView.swift`'s `previousStep`/`bottomNavBar` switches rewired accordingly, and the
old `Step 6/7` `// MARK:` comments renumbered to `7/8` to keep the sequence honest. New
`guidedAccessStep` view explains the gap in plain language (admin PIN protects the app's settings,
not the device itself), then gives the exact three manual steps (Settings → Accessibility →
Guided Access → on; set a passcode or Face/Touch ID; triple-click the side/Home button once the
kiosk is running, every session) — deliberately advisory-only and skippable via the same
"Next"-always-enabled pattern the rest of this wizard already uses for non-blocking steps, since
there's nothing to gate on: `UIAccessibility.isGuidedAccessEnabled` only reflects an active
session, not the Settings-level toggle, so it would read false at this point in setup regardless of
whether the operator actually did anything. No live status shown here for that reason — the
existing `KioskView.swift` warning caption (unaffected, unchanged) remains the only place this app
can ever detect and react to Guided Access state, once the kiosk is actually running. Build-verified
via `xcodebuild ... build` (clean). No relay or Android changes — Android has no Guided Access
equivalent to mirror this into.

**"Both off = free" explanation made always-visible on every Payments surface (2026-08-17), all**
**three repos — two-step design, first attempt was the wrong shape.** The "Both off — all requests
auto-accepted for free" line already existed everywhere (kiosk-native `AdminScreen.kt`/
`AdminView.swift`, `static/admin.html`, `static/bartender.html`) but was purely reactive —
conditional on the toggles already both being off, so an operator only ever saw it *after* leaving
both off, never as a heads-up beforehand. First reported as "I don't see this text anywhere" (kiosk
screens), traced to this exact conditional gating (confirmed by asking whether both toggles were
off — they were), then the user toggled them off live, saw it appear, and said: "this should be on
all the time!"

First fix attempt added a *second*, separate persistent caption above the toggles (reusing the
wizard's `ApprovalModeStep` intro wording) while leaving the original reactive line as-is below the
toggles — modeled on the wizard's own two-tier pattern (persistent rule statement + reactive
confirmation). This shipped, built clean on both platforms, but the user reported it missing on
"the wizard final screens on both kiosks... which is also the admin kiosk UI" — several rounds of
diagnosis (confirming rebuild status, confirming toggle state) before the user clarified what they
actually meant: the new caption *was* there, correctly, above the currency rows — but they wanted
the **existing orange warning below the toggles** to be the one made permanent, not a second new
line added elsewhere. Lesson: "add an explanation" was underspecified on which of two visually
distinct spots was meant, and the first-attempt design (mirroring the wizard's two-line pattern)
was a reasonable guess but not what was asked for — should have clarified placement before
building, given the ask referenced an existing specific element ("there is an explanation below").

Second, corrected fix: removed the newly-added top-of-card caption entirely on all four surfaces,
and instead removed the conditional gating from the *original* line, making it unconditionally
visible in its original position and styling (orange/yellow, below the toggles) — reworded per the
user's explicit correction from an em-dash to a colon: "Both off: all requests auto-accepted for
free" (`static/bartender.html`'s equivalent: "Both off: requests are free and auto-accepted."). On
`static/admin.html`/`static/bartender.html` this meant removing the JS that was toggling the
`show`/`display:none` state (`updatePaymentUI()`/`updatePaymentToggleUI()`) and marking the element
visible by default in markup instead. Both platforms build-verified
(`:app:compileDebugKotlin`, `xcodebuild ... build`).

**Setup wizard Back/Next now pinned to the bottom on every step, iOS only (2026-08-17).** User:
"some screens feature a Back Next button that is fixed on the bottom of the screen, and content
above is scrollable — yet, some screens feature buttons on the very bottom of the page, that I have
to scroll to." Root cause: `SetupView.swift` already had a single shared `bottomNavBar`, pinned
outside the scrollable per-step content — but three steps (`nameStep`, `pinStep`, `pricingStep`)
instead embedded that same `bottomNavBar` as the *last Section inside their own Form*, so on those
three specifically it scrolled away with the rest of the content instead of staying pinned, exactly
the inconsistency the user hit repeatedly on restart. Fixed by removing those three embedded
Sections and dropping their steps from the exclusion list that suppressed the shared pinned bar —
every step but `.uploading` (which has no nav buttons at all) now uses the one shared, always-pinned
`bottomNavBar`, no per-step duplication. Build-verified (`xcodebuild ... build`). No relay or
Android changes — Android's equivalent wizard screens were never reported as having this
inconsistency and weren't audited as part of this fix.

**Android setup wizard had the identical Back/Next scroll bug — worse, universally not just on**
**some steps (2026-08-17), Android only.** Follow-up audit after the iOS fix above, at the user's
request ("check Android's wizard for the same issue"). Every reachable Android wizard step
(`NameEntryStep`, `AdminPinStep`, `DeviceProtectionStep`, `ApprovalModeStep`, `PricingStep`,
`NetworkModeStep`, `LocalFolderStep`, `SpotifyDeviceStep`, `SpotifyPlaylistStep` — 9 of 11 files
under `ui/setup/`) wrapped its entire content *and* its Back/Next Row in one single
`Modifier.verticalScroll(...)` Column, so the nav buttons scrolled away with the rest of the form
on every single step — a strictly worse version of the iOS bug, which only affected 3 of iOS's 8
steps. `LocalFolderStep` and `SpotifyPlaylistStep` were the worst offenders in practice, since both
can show a long scrollable list (folder browser / Spotify playlists) pushing Next far off-screen.
`SpotifyPlaylistStep` had a doc comment that said the quiet part out loud: "Buttons scroll with the
list, appearing just below the last item."

Fixed with the same shape as iOS: split each step's single scrolling `Column` into an outer
`Column(Modifier.fillMaxSize())` containing an inner `Column(Modifier.weight(1f).verticalScroll(...))`
for content and a nav `Row` as a sibling after it, outside the scroll. `SpotifyPlaylistStep`
needed a different fix shape since its Loaded state used a `LazyColumn` with Back/Next appended as
the list's own last `item` (duplicated a second time for the Loading/Error states, which already
rendered nav correctly outside content) — consolidated to one shared nav `Row` after the `when`
block for all three states, computing `enabled` from the current state instead of hardcoding it per
branch. Two files with a `verticalScroll` reference were found to be **dead code** during this audit
(`SetupSummaryStep.kt`, `FolderPickerDialog.kt` — confirmed via grep, zero call sites for either
composable anywhere in the app) and deliberately left untouched — not reachable, not worth fixing.
`DisplayModeStep.kt` (the wizard's first step) has no persistent nav row at all by design (each
option card advances immediately on tap) and needed no change. Build-verified via both
`:app:compileDebugKotlin` and the fuller `:app:assembleDebug`. No relay or iOS changes — this pass
was Android-only, mirroring a fix iOS already had.

**Bartender PIN field's keyboard couldn't be dismissed on the iOS live Admin screen (2026-08-17),**
**iOS only.** `AdminView.swift`'s "New bartender PIN" `SecureField` uses `.keyboardType(.numberPad)`
— iOS's numeric keypad has no Return/Done key of its own by design, so with no other dismiss
mechanism wired up, the keyboard stayed on screen indefinitely once focused, with no way to get rid
of it. Fixed by adding a `@FocusState private var bartenderPinFocused: Bool`, binding it to the
field via `.focused($bartenderPinFocused)`, and adding a `ToolbarItemGroup(placement: .keyboard)`
with a "Done" button to the Form's existing `.toolbar` block that clears it — the same idiom
`SetupView.swift`'s currency field already established elsewhere in this app for exactly this
class of no-Return-key field. Also clears focus automatically from `saveBartenderPin()` itself, so
tapping Save (not just the keyboard's Done) drops the keyboard too. Audited the rest of
`adminPanel`'s `Form` for the same pattern (grepped for `SecureField`/`TextField`/`keyboardType`
within it) — this was the only field affected, so no broader sweep was needed. The "should show
dots as I type" part of the original bug report needed no fix — `SecureField` already masks input
with dots natively; only the missing dismiss mechanism was a real bug. Build-verified
(`xcodebuild ... build`). No relay or Android changes — Android's equivalent bartender PIN field
uses a different (non-SwiftUI) IME that isn't subject to this iOS-specific numberPad quirk.

**Android Spotify Playlist step's nav row was missing the system nav-bar inset, follow-up to the**
**pinned-nav-bar fix above (2026-08-17).** User: buttons on that step "lean into the nav bar below
it," unlike every other wizard step. Root cause: `SpotifyPlaylistStep.kt`'s nav `Row` never had
`.windowInsetsPadding(WindowInsets.navigationBars)` — every other step's nav row already had it
(`NameEntryStep`, `AdminPinStep`, `DeviceProtectionStep`, `ApprovalModeStep`, `PricingStep`,
`NetworkModeStep`, `LocalFolderStep`, `SpotifyDeviceStep`), this one file was the one omission, and
it predated the same-day pinning fix — consolidating the three previous nav render sites in that
file carried the gap forward rather than introducing it. Fixed by adding the same inset modifier,
matching every sibling step exactly. Build-verified (`:app:compileDebugKotlin`). No relay or iOS
changes.

**Bartender Access PIN control simplified on both kiosk-native Admin screens (2026-08-17), iOS +**
**Android.** User: "way too much content for this one setting" — the old design permanently showed
a two-branch on/off status paragraph, an always-visible text field, an always-visible (but
disabled-until-4-digits) Save button, and — only when a PIN was already set — a separate "Turn Off
Bartender Access" button that opened its own inline confirm row. Redesigned to a single control:

- **A set PIN displays as a masked, tap-to-edit placeholder** (`•` repeated
  `bartenderPinDisplayLength` times, default 4) instead of a persistent status sentence — tapping
  it clears into an editable field, exactly the "click on the field would get rid of the ****"
  behavior asked for. No PIN set → the field is simply empty and already editable, no tap needed.
- **Save is not a permanent fixture** — it only renders once there's an actual pending change:
  `hasChange = isEditing && (entry.isEmpty ? currentlyOn : entry.length >= 4)`. Typing 1–3 digits
  (not yet actionable) shows nothing; clearing an existing PIN back to empty *is* treated as a real
  pending change ("no digits = no bartender access", the user's own framing) and surfaces Save
  labeled "Save (turns off bartender access)" rather than the plain "Save" a new/changed PIN gets.
  Tapping it in the empty case reuses the existing destructive confirm dialog (unchanged) rather
  than disabling immediately — still a two-step action for something that immediately kicks any
  paired bartenders.
- **The separate always-visible "Turn Off Bartender Access" button is gone** — folded into the
  same field+Save flow above, one control instead of two.
- **Footer text collapsed to one line**, reusing wording close to the user's own suggested copy:
  "Enable bartender access by setting a PIN, or leave it empty to disable."

`bartenderPinDisplayLength` is session-local only — a stored value is a SHA-256 hash, which carries
no length, so a fresh screen load with an existing PIN falls back to a generic 4-dot placeholder;
only immediately after a same-session Save does the mask reflect the actual digit count typed.
iOS: new `@State private var isEditingBartenderPin`/`bartenderPinDisplayLength`, wired through the
existing `@FocusState bartenderPinFocused` from the keyboard-dismiss fix above. Android: same two
new `remember { mutableStateOf(...) }` locals inside the `AdminCard`. Both platforms build-verified
(`xcodebuild ... build`, `:app:compileDebugKotlin`). No relay changes — purely local UI state, no
wire-contract involvement. LAN `admin.html` (both platforms) and render `static/admin.html` have
the **same** overloaded pattern (status text + always-visible field/Save + separate always-visible
Turn Off button, confirmed by reading `static/admin.html` directly) but were deliberately **not**
touched — user scoped this explicitly to "both platforms," meaning iOS/Android native, not a
request to sweep every surface. Worth revisiting there later for the same simplification, not done
as part of this pass.

**"List on JukeBar map" switch style unified with the Payments switches (2026-08-17), Android**
**only.** User noticed `NameEntryStep.kt`'s Discovery toggle looked different from Stripe/Bartender
Pay on the same platform — checked CLAUDE.md and git history first, found no documented reason for
the divergence, just an unreconciled one-off: black thumb + solid Coral track, vs. every Payments
switch (`AdminScreen.kt`'s Stripe/Bartender/Accepting-requests, `ApprovalModeStep.kt`'s wizard
toggle) using Coral thumb + translucent (40%) Coral track. Unified to the latter. Build-verified
(`:app:compileDebugKotlin`). Android only — not extended to iOS: while checking whether iOS had the
same problem, found `AdminView.swift`'s Payments toggles explicitly set `.tint(.orange)` while
`SetupView.swift`'s "List on JukeBar map" `Toggle` has no explicit tint at all, inheriting the
app-wide accent (`ContentView.swift`'s root `.tint(...)`, a peach/Coral color) instead — a similar
*shape* of inconsistency to the one just fixed on Android, but not identical and not yet confirmed
visually distinguishable on-device. Flagged to the user, not fixed — this task was scoped to
Android only ("on Android" in the original question), and fixing an unrequested iOS issue wasn't
part of what was asked.

**Admin screen header split into two non-scrolling rows so a long bar name can't force truncation**
**(2026-08-17), both platforms — this is the wizard's SUMMARY step too, same composable.** User:
"do not truncate to force onto a single row." Both platforms previously crammed logo + "JukeBar" +
bar name + "Admin" label + Done button into one single-line row — Android via a plain `Row` with no
wrap capability (long names would overflow past the Done button), iOS via a toolbar `.principal`
item with `.lineLimit(1)` (long names literally truncated with an ellipsis, since a nav-bar toolbar
item can't grow taller). Fixed identically on both: row 1 is logo + "JukeBar" + bar name (Android:
`Modifier.weight(1f, fill = false)` on the name `Text` so it wraps within available width instead
of overflowing; iOS: no `lineLimit` at all, so `Text` wraps naturally), row 2 is "Admin" + Done,
`SpaceBetween`. **iOS needed a bigger structural change than Android**: a toolbar `.principal` item
is fixed-height and can't reasonably hold two rows, so the header was pulled out of the toolbar
entirely into a plain `VStack` above the `Form`, with `.toolbar(.hidden, for: .navigationBar)` on
the whole `adminPanel` and the Done button now a plain `Button` in the custom header instead of a
`ToolbarItem(placement: .confirmationAction)`. The keyboard-dismiss `ToolbarItemGroup(placement:
.keyboard)` from the bartender-PIN fix earlier this session is unaffected — keyboard accessory
toolbars attach to the responder chain, not the navigation bar, so hiding the nav bar doesn't
touch it. Android's change was smaller — same `Column`-based header as before, just split into two
rows instead of one `Row`. Both platforms build-verified (`xcodebuild ... build`,
`:app:compileDebugKotlin`). No relay changes.

**Two small iOS-only polish fixes (2026-08-17), both requested together.**

1. **Admin header logo enlarged to match the customer/kiosk page.** `AdminView.swift`'s header
   logo (added earlier this session as part of the two-row header fix) was `size: 22` — small next
   to `KioskView.swift`'s customer-facing `topBar()`, whose `logoSize` is dynamic but caps at
   `isIPad ? 104 : 80`. Added the same `@Environment(\.horizontalSizeClass)`/`isIPad` pattern
   `KioskView.swift` already uses, and matched those capped values exactly rather than replicating
   `topBar()`'s full dynamic-height formula (Admin's header is a fixed compact bar, not a
   variable-height container, so there's no equivalent height to derive a dynamic size from).
   Deliberately logo-only per the request — the adjacent "JukeBar"/bar name text sizes were left
   untouched, not scaled up to match, since only the logo size was asked for.
2. **Guided Access warning caption shortened.** `KioskView.swift`'s `topBar()` warning (shown when
   `kioskDisplayMode != .remoteOnly` and Guided Access isn't active) read "⚠︎ For kiosk security,
   enable Guided Access (Settings → Accessibility), then triple-click the side button" — user:
   "the warning should simply be 'Enable Guided Access!' - the rest is not on the screen [i.e. not
   visible/needed]." Shortened to "⚠︎ Enable Guided Access!", `lineLimit` dropped from 2 to 1 since
   it now always fits on one line. `topBar()` is a single shared function called from all 4 kiosk
   layout variants (portrait/landscape × two contexts), so this was one edit, not four. The fuller
   explanation (Settings path, triple-click gesture) still lives in the setup wizard's dedicated
   Guided Access step from earlier this session — this caption was always meant as an in-the-
   moment nudge, not the primary place to learn the mechanic.

Both build-verified (`xcodebuild ... build`). No relay or Android changes — both fixes were scoped
to iOS specifically by the user, and Android has no equivalent runtime warning caption (Screen
Pinning's advisory lives only in the setup wizard, not on the live kiosk screen).

**Render `bartender.html` now collects a name before the PIN, closing a real multi-bartender gap**
**(2026-08-17), relay only.** Found while confirming what the user had tested: LAN `bartender.html`
(both platforms) has always asked "Your name" before pairing, feeding `BartenderRecord`/
`LocalBartender`'s `name` field — but render's `bartender.html` only ever sent `{pin_hash, role}`
to `bar_authenticate()`, even though the relay backend already reads and stores `body.get("name")`
(`main.py`'s `bar_authenticate()`, falling back to the literal string `"Bartender"`) and the
Bartender Sessions tab is built to display that name per-session. Since the frontend never
collected one, every internet-authenticated bartender showed up identically as "Bartender" —
user, on realizing this: "without taking names we will have a clusterfuck.. How would I know which
session to kill if there is a hacker bartender?" Fixed by adding the same "Your name" field to
`static/bartender.html`'s PIN screen (styled as a lighter-weight `.name-field`, distinct from the
heavily-styled numeric `.pin-field`), sent as `name` in the `/authenticate` POST body — **no
backend change needed at all**, since `bar_authenticate()` already accepted and stored this field;
the gap was purely that render's own frontend never asked. Optional, same "Bartender" fallback the
backend already had if left blank, matching LAN's permissiveness exactly. Not build-verified via a
compiler (`static/*.html` is plain markup/JS, no build step) — reviewed directly instead.

**Correction: only kiosk-native admin screens actually render a bartender QR image — render and**
**LAN `admin.html` never have, confirmed 2026-08-17 by direct grep, not assumption.** I'd told the
user QR codes appear on 3 places (kiosk-native, LAN admin.html, render admin.html); they pushed
back that it's kiosk-native only. Checked directly rather than re-asserting from memory:
`AdminView.swift` has `QRImageView` (×4), `AdminScreen.kt` has `generateQrBitmap`/zxing's
`QRCodeWriter` (×5) — both genuinely render a QR bitmap for the Bartender/Admin URL. `static/
admin.html` has zero matches for any QR-generation pattern; `WebApps/admin.html`'s 3 "canvas"
matches turned out to be an unrelated iOS-Safari video-keepalive hack, not QR. Both render and LAN
`admin.html` only ever show status text ("bartender QR code and bartender page are active") — no
image, and **no visible link either** — there is currently no way to get the actual bartender URL
from render or LAN admin.html at all, only by physically going to the kiosk's own screen. Not fixed
— flagged to the user as a real gap worth deciding on, not silently patched. Also corrected a
test-plan annotation from the immediately-preceding session turn that had repeated this same wrong
"QR appeared on render" claim (`docs/full_system_test_plan.md`, the "Admin sets a bartender PIN
from each of the 5 admin surfaces" checkbox) — the underlying PIN/pairing mechanics the user
actually tested (status flipped to "on," login via the bartender.html URL succeeded) are still
correctly recorded as confirmed; only the "QR appeared" wording was wrong and has been struck.

**Bartender QR image added to render and LAN admin.html's Sessions tab, closing the gap from the**
**correction above (2026-08-17), all three repos.** User: "let remote admin surfaces carry the
bartender QR code so they can show it to bartenders without having to mess with the kiosk... Do
it!" Deliberately reused each platform's already-proven QR generator rather than writing a new
JS QR encoder from scratch (correctness risk for a hand-rolled implementation — Reed-Solomon/mask
selection is easy to get subtly wrong) or loading a third-party JS QR library from a CDN (LAN pages
must work with zero internet dependency by design):
- **Relay** (`main.py`): new `GET /api/bar/{id}/bartender_qr.png`, admin-token-gated like the
  report endpoints. Uses the `qrcode` Python library (new dependency, `qrcode[pil]` added to
  `requirements.txt`) server-side — round-trip verified before shipping (encoded a URL, decoded
  the resulting PNG back with OpenCV's `QRCodeDetector` in a throwaway venv, confirmed it matched
  exactly) rather than trusting an unfamiliar library blind.
- **iOS LAN** (`LocalServer.swift`): new `GET /api/admin/bartender_qr.png`, same admin-token gate.
  Reuses `QRCodeView.swift`'s existing CoreImage `CIFilter.qrCodeGenerator()` logic — extracted
  the generation code into a shared top-level `makeQRImage()` function so the SwiftUI view and the
  new PNG-serving endpoint call the exact same, already-working code path, not a second
  implementation.
- **Android LAN** (`LocalServer.kt`): new `GET /api/admin/bartender_qr.png`, same gate. Reuses
  `AdminScreen.kt`'s existing `generateQrBitmap()` (zxing) directly via import — same reasoning,
  zero new QR logic.
- **Placement, all three**: initially added under the Bartender Access card (where the PIN is
  set), then the user redirected mid-implementation — "the QR codes should go under the Sessions
  tab above the already connected sessions" — moved to a new card at the top of the Sessions
  tab/pane, shown/hidden by the same `bartenderAccessEnabled` state already driving the existing
  status text, with the `<img src>` cache-busted (`&t=Date.now()`) on every show so toggling
  access off-then-on always fetches fresh rather than risking a stale browser-cached image.

**Bartender Sessions list re-sorted to chronological (earliest-first), same pass — closes a**
**latent iOS-only ordering bug found while implementing.** User: "sessions should be listed in
chronological order of sign in, earliest first, latest last (possible duplicates, hackers[...])" —
the earliest sign-in under a given name is presumed legitimate; a later duplicate (re-sign-in or
an impostor) should sort to the bottom where it stands out, not jump to the top. Relay's
`bartender_sessions()` was doing the opposite (`sorted(..., reverse=True)`, latest-first) — flipped
to ascending. **Android was already correct without any change**: `LocalRequestManager.
listBartenderSessions()` returns a `CopyOnWriteArrayList` in append order with no sort applied at
all, so pairing order (= chronological) was already what shipped. **iOS had a real, previously
undiscovered bug**: `LocalServer.swift`'s `/api/bartender/sessions` read `storage.loadBartenders()`,
which lists a directory (`FileManager.contentsOfDirectory`) — filesystem directory listings have
no guaranteed chronological order at all, unlike Android's in-memory list. Added an explicit
`.sorted { $0.pairedAt < $1.pairedAt }` — this had been silently unordered (whatever order the
filesystem happened to return) since the Sessions tab shipped 2026-08-02, never caught before now
because nobody had paired enough bartenders in one session to notice. `_bartender_lockouts`'
"Waiting to Retry" sort (`locked_until`, still descending) was deliberately left untouched — that's
a different list sorted for a different reason (soonest-to-retry visibility), not "sign-in order,"
and the user's request was specific to sessions.

All three platforms build/import-verified (`xcodebuild ... build`, `:app:compileDebugKotlin`,
direct Python import with the new `qrcode` dependency installed).

**Bartender's own name shown on their own bartender.html screen; "Updated" timestamp relocated out**
**of the header (2026-08-17), all three surfaces.** User's round of live testing on render (names
propagate to admin, multiple bartenders can log in, Kill works) surfaced two real gaps: "the
bartender's name is NOT displayed on their own screen - it should - right after [bar name] -
[bartender name]; use diff color for bartender name; the text Updated [datetime] next to bar and
bartender name should go onto the scrollable area top... make sure you do these changes also to the
lan-based bartender/admin pages."

- **Render `bartender.html`**: `.header-role` (an existing but `display: none`'d div right below
  the bar name) was repurposed to show the bartender's own name in the peach accent color
  (`var(--accent)`) instead of staying hidden — set from `verifyPin()`'s already-known `name`
  variable at login (no server round-trip needed, the client already sent it), and persisted
  alongside the cached auth token in `sessionStorage` (`jb_name_{id}_{session}`) so it survives the
  cached-token reconnect path on page reload, not just a fresh login. The `.refresh-badge`
  ("Updated [time]") moved from the sticky `.header` into `#requests-pane` as its first child —
  previously pinned next to the bar name permanently, now scrolls away with the rest of the content
  like everywhere else in this app's design language.
- **iOS/Android LAN `bartender.html`**: structurally different from render's (no persistent
  sticky-header timestamp to move at all — `#last-refresh` was *already* inside the scrollable
  `.refresh-row`/`#reqs-pane`, confirmed by reading the code before assuming a fix was needed, so
  the "Updated" part of this request needed zero changes on either LAN page). The name-display part
  did need fixing: `#header-sub` existed but was actively cleared to empty on iOS
  (`subEl.textContent = ''`) and only ever showed the bar name (not the bartender's own name) on
  Android. Both platforms' `/api/bartender/pair` and `/api/bartender/status` responses gained a new
  `"name"` field (previously the server already stored the paired bartender's name but never
  returned it to that same bartender's own client) — iOS's `updateHeaderTitle()` now sets
  `#header-sub` to `– {name}` instead of clearing it; Android's appends `– {name}` after the
  existing bar-name text it was already showing there. Both reuse `header p`'s pre-existing peach
  color (`#fbbe84`) — already visually distinct from the white/rainbow title, so no new styling
  needed to satisfy "use diff color."

All three build/compile-verified (`xcodebuild ... build`, `:app:compileDebugKotlin`; render is
static HTML/JS, reviewed directly). No relay Python changes — `main.py`'s `bar_authenticate()`
already accepted and stored `name` from the earlier same-day fix, this pass only touched what the
client displays with it.

**iOS: a real "no auto-restore" gap silently rotated the session on every cold app launch,**
**invalidating every bartender token and the customer QR without anyone choosing End Session**
**(fixed 2026-08-18), iOS only.** User: "the bartender session may expire early... I do not
remember closing the kiosk app, yet... it said the session is no longer valid" — asked me to
confirm bartender sessions only expire via an explicit new session or an explicit Kill, as
designed. Traced end to end, not assumed: `AppState.swift`'s `init()` had a comment stating outright
"no auto-restore" — `isSetupComplete` defaulted `false` and nothing anywhere else ever set it back
to `true` except `finishSetup()` itself. This means **every cold process launch** — iOS killing the
app in the background under memory pressure, a crash, a device reboot, not just an intentional
force-quit — landed back on the setup wizard from scratch, pre-filled from `savedConfig`. Tapping
through a pre-filled wizard looks harmless, but its Finish button calls
`LocalStorage.startNewSession()`, which mints a brand-new session id and archives/wipes the old
one — silently invalidating every paired bartender's token (`bar.bartender_tokens` gets replaced
wholesale on the relay once it sees a different `session` value) and the customer-facing QR link,
with no "End Session" ever having been chosen. `_validate_session()`'s exact error
("Invalid session") is the literal message the user saw.

**Android never had this bug** — confirmed by reading `MainActivity.kt`'s `restoreSetupState()`,
which already reads `isSetupComplete = setupPrefs.getBoolean("complete", false)` plus the
persisted `session_id` itself from SharedPreferences on every launch. iOS had no equivalent at all.
Fixed by adding the same shape of restore to `AppState.init()`: if a saved `BarConfig` + session
exist on disk, restore `hostConfig` and set `isSetupComplete = true`, letting `KioskView`'s own
`.task` → `startServerIfNeeded()` handle actually starting the server exactly as it already does on
any other launch — deliberately did **not** duplicate that server-starting logic inline, and
deliberately did **not** set `showAdminAfterSetup` (that's a first-time-setup nicety, not wanted on
an ordinary resume).

**A second, less obvious problem surfaced while building this and was caught before shipping, not**
**after**: `resetSetup()` (End Session) intentionally leaves the underlying `BarConfig`/session
files on disk untouched — it only clears in-memory `hostConfig`/`isSetupComplete`, because the
wizard pre-fills from those same files. That means "does a config+session file exist" alone can't
distinguish "genuinely live, resume me" from "End Session was just chosen, wizard hasn't been
re-finished yet" — using presence-of-files as the sole signal would have resumed an *ended* session
if the app were killed in that specific window. Added an explicit persisted flag,
`setup_complete_v1`, set `true` at the end of `finishSetup()` and `false` in `resetSetup()`, checked
*in addition to* the files existing. **Also handled the upgrade case**: an install already live
before this fix won't have the new flag set yet — added a one-time migration (flag absent →
infer completion from the old file-existence heuristic, then backfill the flag) so upgrading
doesn't spuriously bounce an already-live bar to the wizard once on its first post-update launch.

Build-verified (`xcodebuild ... build`). No relay or Android changes — Android was already correct,
confirmed by reading the code rather than assumed innocent by default.

**Reverted the iOS session-survives-app-kill fix above, same day — user's explicit design call,**
**not a bug in the fix itself.** After shipping it, the user gave two pieces of new information
that together fully overturned the premise: (1) "I have seen these errors on iOS, meaning, the
host must have been on Android" — the original report's host was Android, not iOS, so the iOS fix
was never actually the explanation for what they saw; and (2) an explicit, unambiguous design
call: "if the session restarts (either by me or reloading the app, or anything), the session
RIGHTFULLY should end... there is just no way we should try to managing keeping an ongoing session
during this time in our complex system... so kiosk session restart should wipe the sessions and
force a new session." This directly overturns the fix's whole premise (that a restart should be
survivable) — reverted in full: `AppState.swift`'s `init()` is back to the original "no
auto-restore" comment/behavior, `setupCompleteKey` and its three call sites (`init()`, `finishSetup()`,
`resetSetup()`) all removed. Android's already-correct `MainActivity.restoreSetupState()` was never
touched by any of this and remains as-is — **not because it's now considered wrong**, but because
reverting it would be a separate, unrequested behavior change on a platform the user didn't ask
about; this note exists so a future pass doesn't assume Android needs to match iOS's now-reverted
"no restore" behavior for consistency's sake — the two platforms are deliberately left asymmetric
here, matching the user's Android-specific "session restart should force a new session" framing
without touching working Android code.

**The actual likely cause of the original report: Render auto-deploys on every push to jukebarweb's**
**`main` branch, and this session pushed to `main.py` repeatedly while the user was live-testing** —
each deploy restarts the `uvicorn main:app` process, wiping `_bars` (in-memory only, documented
throughout this file and `docs/architecture.html`) entirely, including every `bartender_tokens`
entry, with zero involvement from either host platform. This fully explains "I do not recall any
changes to the host" — there weren't any; the relay was what restarted. Confirmed via `render.yaml`
(a standard Render web service, no explicit auto-deploy override) plus the sheer volume of `main.py`
pushes this same session (bartender QR endpoint, sort-order fix, name field, etc.) landed while
testing was ongoing. **Accepted as expected, by-design behavior, not fixed** — this matches the
user's own just-stated position that session continuity across any kind of restart isn't something
worth engineering resilience for. Practical lesson for future sessions: avoid pushing to jukebarweb's
`main` while the user is actively live-testing a session, since every push is a relay restart.

Both iOS build-verified after the revert (`xcodebuild ... build`). No relay or Android changes.

**Bartender names must be unique among currently-active sessions for a bar (2026-08-18), all three**
**pairing backends.** User tested creating two bartender sessions under the same name ("Ted") on
Android LAN — the system let it through; asked me to check and fix on every platform, "also on lan
bartender pages and kiosk back end." This directly serves the reason names were added in the first
place (so Kill can reliably target the right person) — two active "Ted"s makes that impossible.
Fixed identically in all three independent pairing implementations:
- **Relay** (`main.py`'s `bar_authenticate()`, role == "bartender" only — admin has no such
  ambiguity concern): case-insensitive check against `bar.bartender_tokens.values()` before minting
  a token, `HTTPException(409, ...)`.
- **Android LAN** (`LocalRequestManager.pairBartender()`): case-insensitive check against the
  in-memory `bartenders` list before adding, new `PairResult.Failure.nameTaken` flag surfaced by
  `LocalServer.kt`'s handler as NanoHTTPD's `Response.Status.CONFLICT` (409).
- **iOS LAN** (`LocalServer.swift`'s `/api/bartender/pair`): case-insensitive check against
  `storage.loadBartenders(sessionId:)` before creating a `BartenderRecord`, `.raw(409, "Conflict",
  ...)`.

All three check only *currently-active* sessions, not history — confirmed for each platform that
Kill genuinely removes the record rather than just marking it (relay: `del bar.bartender_tokens[...]`;
Android: `bartenders.removeIf { ... }`; iOS: `storage.deleteBartender()` removes the file), so a
freed-up name becomes available again immediately, not permanently reserved. None of the three
client pages (`static/bartender.html`, both LAN `bartender.html` files) needed JS changes for the
error path on Android/iOS — both already had a generic `if (!r.ok) throw new Error(d.error || ...)`
fallback that picks up the new `{"error": "..."}` body automatically; only render's `verifyPin()`
needed an explicit `409` branch added (its error handling was more branch-per-status than
Android/iOS's generic fallback). All three backends build/import-verified
(`xcodebuild ... build`, `:app:compileDebugKotlin`, direct Python import).

**Bartender name is now required, minimum 2 characters, all three pairing backends + all three**
**bartender.html pages (2026-08-18).** Found in the same testing round as the uniqueness fix
above: render's bartender login "allowed me to log in without giving a name" — every backend still
silently defaulted a blank name to the literal `"Bartender"`, which defeats the point of collecting
one in the first place (the very first person to leave it blank becomes an unidentifiable generic
"Bartender" on the Sessions tab, exactly the ambiguity this whole feature exists to prevent). User's
follow-up refinement: "length should be 2 or greater; not 4" (some client-side PIN-length gating
code was `< 4`, for the *PIN* field — this clarified the *name* minimum is a separate, smaller
threshold, not a copy-paste of the PIN one) — and "all surfaces and platforms."

- **Relay** (`main.py`): `bar_authenticate()`, role == "bartender" only, rejects `len(raw_name) < 2`
  with `HTTPException(400, "Name must be at least 2 characters")` — the old `or "Bartender"`
  fallback is gone entirely, replaced by this hard requirement.
- **Android LAN** (`LocalRequestManager.pairBartender()`): new `PairResult.Failure.nameRequired`
  flag, checked before the uniqueness check (same order as the relay: PIN validity → name length →
  name uniqueness), surfaced by `LocalServer.kt` as `Response.Status.BAD_REQUEST`.
- **iOS LAN** (`LocalServer.swift`'s `/api/bartender/pair`): `guard rawName.count >= 2 else { ... }`
  returning `.raw(400, ...)`, same ordering.
- **All three `bartender.html` pages** gained matching client-side gating — previously render's Pin
  button was already gated on PIN length but not name length at all, and both LAN pages' Pair
  buttons had **no gating whatsoever** (always clickable, even with both fields empty). All three
  now disable their submit button until the name field has ≥2 trimmed characters (render also still
  requires PIN length ≥4, unchanged), and all three surface the new 400 response's `error` message
  on submission as defense-in-depth against a direct API call bypassing the disabled button.

All three build/import-verified (`xcodebuild ... build`, `:app:compileDebugKotlin`, direct Python
import). Ordering note for future reference: PIN check → name-length check → name-uniqueness check,
consistently across all three backends — a wrong PIN never leaks whether a name would also have
been rejected, and a too-short name never leaks whether it's also already taken.

**LAN admin.html's standalone "System" tab merged into "Sessions," both platforms (2026-08-18) —**
**closes a real discoverability gap, not just a naming mismatch.** User: LAN admin had "no Session
tab but have System tab instead (that render does not)" and separately flagged the bartender QR
"is not present on the LAN admin page despite your earlier claims" — checked directly and the QR
*was* already there (added earlier this session, confirmed via `git log` timestamps predating the
report), inside a "Sessions" tab that genuinely did already exist too. The actual bug: LAN
admin.html has always had **5** tabs (Requests/Reports/Actions/**System**/Sessions) while render
has only **4** (no System at all) — the unfamiliar extra "System" tab, sitting directly before
Sessions in the tab order, was different enough from render's layout that the user's own live
testing landed there first and concluded the QR/session content wasn't there at all, since System's
panel (server/session stats, bar config, Stripe status) has nothing to do with bartenders.

User's fix direction, followed exactly: rename/merge System into Sessions rather than keep both —
"push below session content after renaming System tab Session... QR code... on top of Session tab,
then the list of bartender sessions, then the current System tab content underneath." Implemented
on both `assets/admin.html` (Android) and `WebApps/admin.html` (iOS) identically:
- Removed the standalone `System` `<div class="tab">` button and its `#tab-system` panel entirely.
- The System panel's sole content div (`#system-info`) moved to the bottom of `#tab-sessions`,
  below the existing QR card → Active Bartender Sessions → Waiting to Retry stack (which was
  already in the right order — the QR-not-present complaint traced to the wrong tab, not wrong
  content).
- `switchTab()`'s tab array dropped `'system'`; `loadSystem()` (unchanged function, still targets
  `#system-info` by id) now fires alongside `loadBartenderSessions()` whenever the Sessions tab
  opens, instead of only on its own now-removed tab.

Tab count on both LAN pages is now 4 (Requests/Reports/Actions/Sessions), matching render exactly.
Pure static HTML/JS on both platforms — no Kotlin/Swift touched, no build step applicable beyond
reviewing the diff directly (both files' structure double-checked for balanced markup after the
edit). No relay changes.

**LAN bartender.html showed "Your access was ended by the admin" before ever pairing, both**
**platforms (2026-08-18) — a real bug, not a messaging tweak.** User: "on lan bartender login page
I entered correct pin but short name - system responded by: Your access was ended by the admin -
enter the pin again to continue; which is misleading message; it was because of short name
entered." Traced precisely rather than assumed: the short-name rejection itself was working
correctly (client-side `doPair()` already has `if (name.length < 2) return;`, confirmed by direct
re-read) — the misleading message came from somewhere else entirely, running in the background
regardless of what the user was doing. Both platforms' `bartender.html` have `setInterval
(loadRequests, 15000)` registered **unconditionally at page-load time**, not gated to only start
once actually paired (unlike its sibling `loadPaymentState`'s interval, which correctly only starts
inside `showMain()`). `loadRequests()` itself sends `token=${bartenderId || ''}` with no guard — on
a freshly-loaded, never-paired page `bartenderId` is empty, the server correctly 401s an empty
token, and `bartenderKicked()`/`sessionKicked()` fires its "ended by the admin" message — a message
that only makes sense for a *previously valid* session that got revoked, firing here for a session
that never existed at all. The user's short-name PIN entry was real but incidental — this interval
was going to fire this exact wrong message on *any* freshly-loaded, not-yet-paired LAN bartender
page within 15 seconds, regardless of what was being typed.

Fixed by adding `if (!bartenderId) return;` as the first line of `loadRequests()` on both
`assets/bartender.html` (Android) and `WebApps/bartender.html` (iOS) — the two legitimate call
sites (`doPair()`'s success branch, `checkStatus()`'s approved branch via `showMain()`) both already
set `bartenderId` before calling it, so this only suppresses the spurious unconditional
interval-driven calls before pairing, no regression to the real flow. Render's equivalent
(`S.pollTimer`) was already correctly scoped inside `startMain()`, itself only reachable after a
successful login — confirmed via direct re-read, no fix needed there. Pure static HTML/JS on both
platforms, no build step applicable.

**Turning off bartender access left existing paired sessions alive indefinitely, all three**
**backends — a real bug, not a UI staleness issue (2026-08-18).** User, testing on render: turned
off bartender access while a bartender session was live, saw the tab eventually show a raw
`{"detail":"Not found"}` JSON blob instead of a friendly message ("your session was ended by admin
or system"), and separately noticed the killed session kept showing as "active" on the Sessions
tab. Also asked whether closing a bartender tab could be detected the same way.

Traced precisely: turning off bartender access clears `bartender_pin_hash`, but nothing in
`host_sync()`'s echo-confirm block (or the LAN/kiosk-native equivalents) ever touched
`bartender_tokens`/`bartenders`/`storage.loadBartenders()` — `_require_bartender_token()` and its
LAN/iOS equivalents only check *token presence*, never whether a PIN is currently set, so an
already-issued token kept authenticating indefinitely. This explains both symptoms at once: the
Sessions tab genuinely still had an active record (nothing had ever deleted it), and a live
bartender tab's own polling (`/api/bar/{id}/requests` etc.) kept succeeding rather than ever
401ing — so the only way to see a "kicked" state at all was a hard page reload, which hit
`bartender_page()`'s pre-existing hard-lockout gate (`bar.bartender_pin_hash` empty → real 404,
2026-08 PIN split, mirrors the customer `localOnly` lockout) and got FastAPI's bare default JSON
error body, since that route had never needed a friendly response before — the gate was built for
"this bar has never had bartender access," not "a legitimate bartender's access was just revoked."

Fixed with two independent changes, all three backends:
1. **Session purge.** Whenever the bartender PIN transitions to empty, immediately delete every
   currently-active bartender-role session: relay (`host_sync()`'s echo block filters
   `bar.bartender_tokens` down to non-bartender roles), Android (`LocalRequestManager
   .purgeAllBartenderSessions()`, called from both `LocalServer.handleAdminSettings()` — the LAN
   remote-admin path — and `MainActivity`'s kiosk-native `onSetBartenderPin` callback), iOS
   (`LocalStorage.deleteAllBartenders(sessionId:)`, called from both `LocalServer.swift`'s
   `/api/admin/settings` handler and `AdminView.swift`'s `clearBartenderPin()`). None of these
   need a host round-trip — this is the same "just delete it now" shape as the existing
   Kill-session action, not something that waits for host confirmation. Internet-mode Android/iOS
   have no local bartender-session store at all (those bartenders pair against the relay
   directly), so only the relay-side fix applies there — confirmed structurally, not assumed.
2. **Friendly 404.** `bartender_page()` (relay), `/bartender` (both LAN `LocalServer`s) now
   return a small styled HTML page ("Bartender access unavailable... Please check with bar
   staff.") instead of a bare JSON/empty 404 body when the PIN is off — still a real 404 status
   (the hard-lockout intent from the PIN-split design is unchanged, no page/feature is exposed),
   just no longer looking like a crash. Deliberately generic wording — doesn't distinguish "never
   configured" from "just turned off," so it reveals nothing more than the plain 404 already did.

**Tab-close detection: confirmed not solvable, not attempted.** No `beforeunload`/`sendBeacon`
kill-call exists on any of the three `bartender.html` pages, and none was added — a browser tab
close is not reliably detectable server-side (network drop, backgrounding, and an actual close all
look identical from the server's perspective; `beforeunload` handlers are unreliable across mobile
browsers specifically). This matches the already-established, explicitly-confirmed design decision
elsewhere in this file ("Bartender pairing has no automatic expiration — confirmed intentional") —
a paired bartender's session is meant to stay valid until an explicit Kill or the bar's own
End Session, not lapse from inactivity or a closed tab. The one thing that *is* now reliably
detectable end-to-end, per this fix, is the admin explicitly turning bartender access off.

**LAN admin.html's Actions/Sessions tabs never live-refreshed — a polling gap, not a propagation**
**bug (2026-08-18), Android + iOS LAN only, render unaffected.** User: enabled bartender access
from the kiosk-native Admin screen (not the LAN admin page) while on WiFi/hotspot transport — the
already-open LAN admin.html kept showing bartender access as off on the Actions tab, and no QR
appeared on the Sessions tab either. Guessed it might be a violation of the "host state broadcasts
itself out" architecture principle — it wasn't; traced the actual data path first rather than
assuming: kiosk-native's `onSetBartenderPin`/`clearBartenderPin` correctly call
`propagateBarDetails()`/`pushPaymentSettings()`, which synchronously update the LAN `LocalServer`'s
own `barDetails` in-process (Android: `localServer.barDetails = details`; iOS: same shape via
`reloadConfig()`). `/api/catalog` (`catalogJson()`) reads that live property fresh on every call —
so the *data* was never stale, confirmed by reading the code, not assumed correct.

The actual bug: both LAN `admin.html`'s `loadActions()` (the only thing that calls
`updateLanBartenderPinUI()`/`updateBartenderPinUI()`, which drives both the Actions tab's toggle
display *and* the Sessions tab's QR-card visibility) was only ever invoked on page load or on an
explicit `switchTab('actions')` click — never on `switchTab('sessions')`, and never on any
recurring timer. Requests and NowPlaying already have their own 5s/3s poll loops; Actions/Sessions
never got one, so any host-side change made while the admin was sitting on either tab (or had
already passed through it once) simply never surfaced until they manually left and came back.
Fixed identically on both platforms: the existing gated `pollTimer` interval (already conditional
on `activeTab === 'requests'` for `loadRequests()`) gained a second condition,
`if (activeTab === 'actions' || activeTab === 'sessions') loadActions();` — reuses the existing 5s
timer rather than adding a new one, and only fetches when one of the two relevant tabs is actually
visible, matching the existing gating idiom instead of polling unconditionally in the background.

**Render's `static/admin.html` already had no equivalent gap, confirmed by reading the code, not**
**assumed safe by default**: its `poll()` runs unconditionally every 5s regardless of `activeTab`
(no gating at all, simpler than LAN's per-tab structure) and its response already includes
`bartender_access_enabled`; `updatePaymentUI()` (called every poll) already ends by calling
`updateBartenderPinUI()`. No render change needed.

**Android: a real startup race could silently drop a kiosk-native settings change made right**
**after Launch Kiosk, Android only (2026-08-18).** User: set the bartender PIN from the kiosk-native
Admin screen right after starting the app — the kiosk's own screen correctly showed the bartender
QR (proof `barDetails.bartenderPinHash` was set), but the LAN admin page kept saying no bartender
access, and scanning the kiosk's own QR led to the just-added "Bartender access unavailable" page.
Traced to a genuine race in `MainActivity.startLocalServer(details: BarDetails)`: it took `details`
as a one-time parameter snapshot, baked it into the newly-constructed `LocalServer` at
`server.barDetails = details`, then ran `localRequestManager.reset(sessionId)` and
`server.startLanServer()` before finally doing `localServer = server`. Android always re-shows the
setup wizard on restart (pre-filled) and only flips `isSetupComplete = true` — making the kiosk
view and its Admin overlay reachable — from a separate, synchronous `onLaunchKiosk` tap that does
**not** wait for `startQueue()`'s coroutine (Spotify playlist fetch, then eventually
`startLocalServer()`) to finish. A PIN set via the Admin screen during that window called
`propagateBarDetails()`, whose `localServer?.let { it.barDetails = details }` found `this.localServer`
still pointing at the *previous* server (or null) — not yet reassigned — so the update landed on
the wrong object and was silently lost the moment `localServer = server` then pointed at the new
object's already-stale baked-in snapshot. The kiosk's own UI never showed anything wrong because it
reads `barDetails` directly, never through `LocalServer` at all — only the LAN HTTP routes
(`checkBartenderAllowed()`, `catalogJson()`, etc.) were affected, since those are what actually read
`LocalServer.barDetails`. Fixed by re-syncing from the *live* `barDetails` immediately after
`localServer = server` (`server.barDetails = barDetails ?: details`) — closes the window regardless
of exactly when a change happens during construction, without needing to touch the trigger sites.
**iOS has no equivalent bug** — checked directly, not assumed: iOS's `LocalServer.swift` route
handlers all call `storage.loadConfig()` fresh from disk on every single request (no in-memory
`barDetails` cache at all), so there's no snapshot to go stale in the first place.

**Related claim, not independently confirmed**: same report also described a LAN admin.html browser
tab left open from before a restart appearing to "keep working" against the new kiosk session.
Investigated `LocalRequestManager.reset(sessionId)` (called every `startLocalServer()`, clears
`adminTokens`/`bartenders`/`sessionToken`) — this should already invalidate a stale admin token on
a genuine restart+relaunch. Public, unauthenticated LAN endpoints (`/api/catalog`, `/api/nowplaying`)
legitimately keep working for anyone on the LAN regardless of session freshness, by design (same as
`customer.html`) — a stale tab continuing to *display* data isn't itself a bug. Whether a stale tab
can still perform an actual *authenticated* action (settings/approve/deny) after a genuine restart
wasn't independently reproduced — flagged to the user to retest specifically for that, since the
race-condition fix above may have been the actual cause of what looked like session staleness.

**Follow-up, same day: the actual bug wasn't a race at all — a genuinely missing propagate call**
**on the wizard's Summary screen, Android only.** User pushed back on the race-condition theory
above with a precise counter-repro: they deliberately took their time on the wizard's Summary
screen (no fast action), connected a phone to the LAN admin page via its QR, *then* set the
bartender PIN on the kiosk, and waited — the LAN admin page never picked it up, no matter how long.
That ruled out timing entirely and pointed at something structurally missing instead of a narrow
window. Found it: `AdminScreen` (the same composable) is wired up from **two different places**
with two different callback implementations — the live post-launch overlay's `onSetBartenderPin`/
`onToggleStripe`/etc. (inside `KioskView`'s block in `MainActivity.kt`) each correctly call
`propagateBarDetails()` after updating `barDetails`, but the wizard's Summary step
(`SetupWizardScreen.kt`) routes all of its own `AdminScreen` callbacks through one shared
`onBarDetailsSaved` handler that only did `barDetails = details; savePartialState()` — **never**
`propagateBarDetails()`. So any toggle made on the Summary screen (not just bartender PIN — Stripe/
Bartender/AcceptingRequests too) updated `barDetails` (which is why the kiosk's own QR correctly
showed the new state — it reads `barDetails` directly) but never reached the already-running
`LocalServer` or `RelayService` at all. Not a window to beat, a permanently missing wire — this is
why waiting never helped. Fixed by adding `propagateBarDetails()` to `onBarDetailsSaved` — safe to
call unconditionally even during earlier wizard steps before any server exists yet, since
`propagateBarDetails()` already no-ops safely when `localServer` is still null. iOS confirmed to
have no equivalent split: `AdminView` reads/writes through the same `AppState`/`LocalStorage`
singleton regardless of whether it's shown from the wizard or live, so there was never a second,
diverging callback implementation to begin with — this class of bug is specific to Android's
callback-wiring structure. Lesson: when the same composable/view is instantiated from two call
sites with hand-written callback wiring at each, drift between them is the default risk, not the
exception — worth grepping for every call site of a shared admin/settings component whenever one
of its actions doesn't seem to propagate, not just the one path most recently touched.

**Bartender Sessions list still needed a manual refresh after the Actions/Sessions live-refresh**
**fix, all three admin surfaces (2026-08-18 follow-up).** User, after confirming the missing-
propagate fix above worked (bartender access toggling now correctly hides/reveals the QR and
status on both kiosk-native and LAN admin, and PIN/name-uniqueness/reuse-after-delete all still
work): noticed the Sessions **list** itself (paired bartenders + lockouts) still required tapping
"↻ Refresh" or leaving-and-re-entering the tab, unlike everything else on that tab which now
auto-updates. Root cause: the earlier same-day fix (extending each page's poll timer to call
`loadActions()` while on Actions/Sessions) only refreshes what `loadActions()` itself drives — the
QR-visibility card and Actions-tab toggles — it never touched `loadBartenderSessions()`/`loadSystem()`,
which are separate functions only ever called from `switchTab('sessions')` or the manual button.
Same gap existed on render too (`poll()` already runs unconditionally every 5s and refreshes the
QR/status via `updatePaymentUI()`, but never called `loadBartenderSessions()`) — user asked to check
render directly rather than assuming it was LAN-only, correctly guessing the same class of bug.
Fixed on all three: both LAN admin.html's poll timers now also call `loadBartenderSessions()` +
`loadSystem()` while on the Sessions tab; render's `poll()` now also calls `loadBartenderSessions()`
under the same `S.activeTab === 'sessions'` condition it already tracks. No iOS/Android Kotlin/Swift
changes — purely the three `admin.html` files (`static/`, both `WebApps/`/`assets/`).

**LAN bartender.html: a stale localStorage credential from a previous bartender could poison a**
**second bartender's fresh login attempt, both platforms (2026-08-18).** User: a second bartender
opening the LAN bartender page on a device that had paired a *different* bartender before, saw
"Your access was ended by the admin" despite never submitting the pair form at all — same message
as the earlier `loadRequests()`-before-pairing bug fixed the same day, but a genuinely different
trigger. Root cause: `bartenderId` is seeded from `localStorage.getItem('bt_id')` at page load —
shared across *any* bartender who has ever paired on this device/browser, not scoped per-login-
attempt. `checkStatus()`'s failure branch (`if (!r.ok) { showPair(); return; }`, hit when
`/api/bartender/status` 404s on an id that no longer exists) correctly fell back to the pair
screen, but never cleared `bartenderId`/localStorage — so the module-level variable stayed set to
the stale, invalid id. The earlier same-day fix's guard (`if (!bartenderId) return;` in
`loadRequests()`) only checks *truthiness*, not *validity* — a stale-but-non-empty id sails right
through it, and the still-unconditionally-armed `setInterval(loadRequests, 15000)` fires moments
later with that same bad id, gets a real 401, and `bartenderKicked()` shows its "ended by the
admin" message for a credential that was never actually revoked mid-session — it just never
belonged to this login attempt to begin with. Fixed by clearing `localStorage`/`bartenderId` in
`checkStatus()`'s `!r.ok` branch too, mirroring exactly what `bartenderKicked()` already does —
both `assets/bartender.html` (Android) and `WebApps/bartender.html` (iOS). Render's equivalent
uses `sessionStorage` (not `localStorage`) keyed by `jukebarId`+`session`, which is scoped per-tab
by browser design — a genuinely new tab starts with no cached token at all, so this specific class
of cross-bartender staleness doesn't arise there; not fixed, not needed.

**LAN admin.html had zero session-expiry detection at all, unlike bartender.html — closed**
**2026-08-18, both platforms.** User asked directly: do LAN admin sessions survive an app/session
restart? Answer traced precisely: **no, not server-side** — `LocalRequestManager.reset(sessionId)`
(called every `startLocalServer()`, i.e. every wizard-completion cycle, which always re-runs on
restart since Android always re-shows the wizard) clears `adminTokens` unconditionally. But
**client-side, a left-open admin.html tab had no way to find out** — unlike `bartender.html`
(which has `bartenderKicked()`, wired into its main poll since day one), `admin.html` had *no*
401-detection anywhere across its ~15 `barConfig.token`-bearing fetch calls. A dead token meant
every admin-gated action just silently failed (`if (!res.ok) return;`/`throw` with no visible
message) — a settings toggle wouldn't visibly flip, the Sessions list showed a generic "could not
load," with nothing telling the operator to re-enter the PIN. This is very likely what produced
the earlier same-day "leftover tab kept working" observation — the *public* endpoints (`/api/catalog`,
`/api/nowplaying`) genuinely don't need a token and kept updating, giving the appearance of a live
session, while any actual authenticated action would have been silently broken.

Fixed by adding a shared `adminKicked()` (mirrors `bartenderKicked()` exactly: clears `barConfig`,
calls `showPinScreen()`, shows an explanatory message) wired into the highest-traffic
admin-token-gated call sites on both LAN `admin.html` files: `loadRequests()` (main 5s poll while
on the Requests tab), `loadBartenderSessions()` (Sessions tab, both periodic-poll and manual-
refresh calls — iOS already had a 401 branch here but it only showed inline text, never actually
logged the admin out), `toggleLanPayment()` (Stripe/Bartender/AcceptingRequests toggles), and the
bartender-PIN save/clear functions. Deliberately **not** exhaustively wired into every single
admin.html fetch (report generation, kill-session, clear-lockout) — those are rarer, one-off
actions where a silent failure is far less confusing (the button visibly does nothing, versus a
toggle that looks like it should have flipped) — scoped to the paths that run continuously or are
tapped often, not a full sweep of every endpoint. Render's `static/admin.html` was not touched —
it already handles the analogous case correctly via its 403 "Session expired" `showError()` path.

**A LAN admin session, once authenticated, could survive a genuine app restart — investigated at**
**length, not fully root-caused, but closed via a reliable mitigation (2026-08-18), both platforms.**
User did a careful, controlled test: swiped the Android app fully off (not just IntelliJ's Run
button, ruling that theory out), reopened it, walked through the wizard start to finish (confirmed
`startQueue()` — which mints a fresh `sessionId` and eventually calls
`LocalRequestManager.reset()`, clearing `adminTokens` — always fires here, it's the only path that
reaches the Summary step) — then, **without reloading** an already-open LAN admin browser tab from
before the restart, toggled a setting. It worked, and the change genuinely reached the kiosk. This
means the pre-restart admin token was still being accepted by whatever was actually answering
requests, despite every code path saying a freshly-constructed `LocalRequestManager` (a plain
instance field, no cross-instance sharing) should have started with an empty `adminTokens` list.

Traced as far as static reading of the code allows: `MainActivity.startLocalServer()`'s first line
(`localServer?.stopLanServer()`) is a safe-call against *that instance's own* `localServer`
property — if the previous `LocalServer`/NanoHTTPD/`LanForegroundService` somehow outlived the
Activity restart (no `android:process` override on the service, so it should die with the app
process; default `stopWithTask` behavior should also stop it on task removal — checked the
manifest directly, found nothing that should prevent teardown), the new instance has no reference
to it and can't stop it. Whether that's actually what's happening, or something else about Android
process/task reuse that isn't fully within app-code control, wasn't conclusively pinned down —
would need live Logcat/PID inspection across a real restart to settle, which wasn't available in
this session.

**User's own reframing, correctly separating the two concerns**: "I am starting to think this is
acceptable in most cases... However if there is a bad admin session, then restarting will not cure
and get rid of it." A convenience-preserving stale session on the *same* trusted device is a minor
UX question; a way to reliably kill a *compromised* admin credential is a real security
requirement, and it shouldn't depend on an OS process-lifecycle detail nobody can fully guarantee.
Rather than continue chasing the exact restart mechanism, closed the actual gap this exposed:
**changing the admin PIN never invalidated already-issued admin tokens, on either platform** — the
existing kiosk-native "Forgot PIN" recovery flow (Android `MainActivity`'s `onPinReset`, iOS
`AdminView.swift`'s `showForgotPin` "Set PIN" button) only ever updated the stored hash going
forward; a token minted before the reset kept working indefinitely regardless. That defeats the
entire point of a PIN-reset-as-security-recovery flow — if you suspect someone unwanted has your
PIN, changing it should also cut off any session they may have already opened with it. Added
`purgeAdminTokens()` to both platforms' admin-token stores (Android
`LocalRequestManager.adminTokens.clear()`, iOS `LocalServer.adminTokens.removeAll()`), called from
both Forgot-PIN flows immediately after saving the new hash. This gives a reliable, in-app-code
mechanism for the user's actual concern, independent of whatever restart/process-lifecycle
uncertainty remains unresolved. Both platforms build-verified
(`:app:compileDebugKotlin`, `xcodebuild ... build`).

**Likely actual root cause found for the stale-LAN-admin-session-survives-restart saga: missing**
**`onTaskRemoved()`, Android only (2026-08-18).** User's response to the PIN-reset mitigation
above was exactly right: "Did you force a pin change over restart? ... If the stale tab will
likely keep working, then what exactly did you change??" — correctly rejecting a workaround for
a problem that deserved an actual fix, and then independently arrived at the right underlying
principle: a session restart should mint a fresh session id and everything connected should have
to reconnect, "that should be the right behavior... but when we do intend to close the session,
that should break all connections, no?" — exactly the standard in-memory-session-invalidates-on-
restart pattern this codebase's own `LocalRequestManager.reset()` is already supposed to implement.

That prompted going back to find why `reset()`'s `adminTokens.clear()` wasn't taking effect for
the actual, currently-serving object. Found a real, standard Android pitfall in
`LanForegroundService.kt` (started by `MainActivity.startLocalServer()` purely to keep the LAN
server's process from being frozen by OEM battery managers — it holds no reference to the actual
`LocalServer`/`LocalRequestManager` at all, by design): it never overrode `onTaskRemoved()` — the
specific Android hook that exists precisely because **a foreground service can outlive its task
being swiped away from Recents**, and it used `START_STICKY`, telling the OS to resurrect it on
its own after being killed, for no reason this service actually needs. Without `onTaskRemoved()`,
swiping the app away doesn't guarantee `MainActivity.onDestroy()` (and thus `stopLocalServer()`)
fires promptly, or at all, before the process is genuinely reclaimed — leaving a real possibility
that the *previous* `LocalServer`'s NanoHTTPD listener (bound to port 8080, holding its own,
never-reset `adminTokens`) keeps answering requests independently of whatever a subsequent app
launch starts. This fully explains every observed symptom: a stale, never-reloaded LAN admin tab
kept working because it may genuinely have still been talking to the *old* listener the whole
time, not a fresh one that somehow inherited old credentials.

Fixed: `LanForegroundService.onTaskRemoved()` now invokes a listener (`onTaskRemovedListener`, a
static callback `MainActivity.onCreate()` points at its own `stopLocalServer()`, re-registered on
every fresh instance) and calls `stopSelf()` — the moment the task is swiped, the real server gets
torn down immediately, not whenever/if `onDestroy()` eventually runs. Switched
`START_STICKY` → `START_NOT_STICKY` since nothing should ever restart this service except
`MainActivity` explicitly asking it to. As a second, independent safety net,
`LocalServer.startLanServer()`'s previously-uncaught `start()` call (NanoHTTPD's own bind, which
throws `IOException`) is now wrapped in try/catch with a clear log line — if a port conflict still
somehow occurs despite the fix above, it now fails loudly and safely instead of propagating an
uncaught exception or silently leaving `localServer` pointing at a server that never actually
bound. Build-verified (`:app:compileDebugKotlin`). Not yet independently confirmed against a real
device repro (would need the user to retest the exact swipe-off scenario) — this is the most
concrete, standard-pitfall explanation found via code reading, not a live-verified fix. iOS not
touched (for this specific race) — its process lifecycle has no equivalent foreground-service/
task-removal ambiguity; a force-quit there reliably kills everything.

**Follow-up, same day: iOS had its own, more unconditional version of the admin-token gap.** User
asked directly whether "similar techniques" existed on iOS. Checked rather than assumed parity —
found a real, worse-than-Android gap: `LocalServer.shared` is a true app-process-lifetime
singleton (unlike Android's per-`MainActivity`-instance `LocalRequestManager`), and its
`adminTokens` Set was **never cleared anywhere**, not even by `AppState.resetSetup()` (End
Session) — `LocalServer.shared.stop()` only stops the Swifter listener, never touches
`adminTokens`. Unlike bartender records (disk-persisted, keyed per `sessionId`, so a new session
naturally can't see an old session's bartender list), admin tokens had no session-scoping or
clearing mechanism at all — an admin token minted in one session stayed valid across *any number*
of End Session → new setup cycles, for as long as the app process itself stayed alive. This isn't
a race like Android's, it's unconditional. Fixed by calling the same `purgeAdminTokens()` added
earlier the same day (for the Forgot-PIN fix) from `resetSetup()` too. Build-verified
(`xcodebuild ... build`).

**"Managing requests" — auto-manage mode for `accepting_requests`, implemented 2026-08-28, all 13**
**UI surfaces + relay.** Closes the design placeholder recorded 2026-08-22 (see that day's open
questions, resolved below). Two mutually exclusive modes for controlling request acceptance, on
every Admin surface (kiosk-native both platforms, both LAN `admin.html`, render `static/admin.html`)
grouped in the same "Requests"/"🚫 Requests" card as the pre-existing Accepting-requests toggle,
per explicit user direction not to scatter it into a new section:
- **Manual** (default, unchanged): the existing Start/Stop toggle, admin-controlled.
- **Auto**: two operator-set watermarks, default **10** (`auto_manage_max` — stop accepting once
  outstanding count reaches this) and **5** (`auto_manage_restart` — resume once it drops to this
  or below). Both zero = inert even if Auto is selected (safety valve before an operator fills in
  real numbers). Outstanding = every live request not yet in a terminal state (pending + approved/
  up-next, i.e. not `played`/`denied`/`unfulfilled`) — resolves the 2026-08-22 open question in
  favor of the broader definition (reflects actual backlog, not just the review queue).
- **Mutual exclusivity enforced, not cosmetic**: selecting Auto greys out the manual toggle
  (read-only, mirrors the existing disabled-but-visible idiom already used for Stripe in Local
  Only mode) so a human tap and the host's own watermark evaluation can never race over the same
  `accepting_requests` field. Selecting Manual fades the two number fields instead of hiding them,
  so the operator can still see the configured values.
- **Switching modes (or editing the thresholds) takes effect immediately** — every apply path
  (kiosk-native direct edit, LAN's synchronous `/api/admin/settings`, and a relay-queued
  `desired_settings` change landing via sync) triggers one evaluation pass right away rather than
  waiting for the next periodic tick.

**Wire contract** — three new fields ride the exact same self-healing pattern as the existing
`bartender_enabled`/`stripe_enabled`/`accepting_requests`/`bartender_pin_hash` settings, no new
concept: `BarSession.auto_manage_requests` (bool, default `False`), `auto_manage_max` (int, default
`10`), `auto_manage_restart` (int, default `5`). Sent top-level in `host_register()`'s body (like
`accepting_requests`), nested under `settings` in every `host_sync()` call (host's unconditional
every-5s echo of its own truth — the relay just trusts and stores it, same as the three bools), and
may appear in a sync response's `desired_settings` for the host to apply. `bar_settings()` requires
an **admin** token specifically for all three (like `bartender_pin_hash` — not exposed on
bartender.html, admin-only by UI convention there too). `bartender_requests()`'s response — what
`admin.html` polls — now also echoes the three raw current values so the UI can render current
mode/thresholds; the existing `settings_pending` list (`= list(desired_settings.keys())`) covers
these three automatically, no separate plumbing needed.

**The evaluation itself is entirely host-side, per the governing "host is bible" principle** — the
relay never computes or overrides `accepting_requests`, it only ever echoes whatever the host
already decided, exactly like the manual toggle always worked. Both host platforms implement one
setter (`AppState.setAcceptingRequests()` / `MainActivity.setAcceptingRequests()`) shared between
the manual UI control and the new `evaluateAutoManageRequests()`/`evaluateAutoManage()` watermark
check, so auto-manage never introduces a second parallel path to the same field. Runs regardless of
transport (internet/WiFi/hotspot) since it's driven by a transport-independent hook on each
platform — iOS: the existing 2s `KioskView` ticker (already ran in every mode, previously only used
to push now-playing/mark-played); Android: a new 5s coroutine loop started alongside playback in
`startQueue()` (not gated to the internet-only branch), independent of whether `RelayService` or
`LocalServer` end up running that session.

**Setup wizard**: new step on both platforms, inserted right after the payment/approval-mode step
(iOS: `.autoManage`, between `.approvalMode` and `.guidedAccess`, wizard `MARK: Step N` comments
renumbered 6→9 to stay honest; Android: `WizardStep.AUTO_MANAGE`, between `APPROVAL_MODE` and
`PRICING`, new `ui/setup/AutoManageStep.kt`) — same Manual/Auto + two-field UI as the live Admin
screen, always-valid defaults so Next never blocks, matching the non-blocking-advisory philosophy
already established for Device Protection/Guided Access.

Both platforms build-verified (`xcodebuild ... build`, `:app:compileDebugKotlin` +
`:app:assembleDebug`) and diffs spot-checked directly against the wire contract above — field
names/shapes confirmed identical between the two independent implementations. iOS's own
`DECISIONS.md` was deliberately left untouched — its "Session log" section stopped being maintained
on 2026-07-22 (confirmed via `git log`), with this CLAUDE.md having been the sole cross-repo record
since; Android's got a dated entry anyway (added mid-implementation before that inconsistency was
noticed) — left in place rather than reverted, but the asymmetry is noted here so a future pass
doesn't assume both repos follow the same convention. All three repos committed and pushed the same
day, per the standing cross-repo push policy — verified independently (fresh `xcodebuild`/
`gradlew` rebuilds + direct diff review on both host repos, not just trusted from the two
implementing forks' self-reports) after one fork's completion report described briefly touching
the other platform's repo mid-task; both repos' working trees came out as single, coherent,
non-duplicated changesets, so nothing was actually corrupted.

**"Outstanding" narrowed to approved/up-next only, same day, closing an abuse angle before it**
**shipped to real users.** User's own review of the exact wizard/kiosk copy above ("outstanding
approved requests") surfaced a mismatch: the copy already said "approved," but every
implementation's counting logic included `pending` too. Thinking through why prompted the real
question — a bad actor could otherwise spam pay-to-bartender requests with no intent to pay or
show up, purely to inflate the outstanding count and trip the stop-accepting watermark, freezing
requests for genuine customers. First instinct was IP-based request throttling per source address
to prevent this; user proposed a simpler, architecturally cleaner fix instead: **exclude `pending`**
**from the count entirely** — a still-pending pay-to-bartender request hasn't been reviewed *or*
paid for yet (a bartender's Approve tap **is** the payment confirmation, per the `payment_method`
design elsewhere in this file), so it can't do anything until a human actually approves it, and
the bartender already sees a suspicious burst of pending requests directly and can deny them. Only
`approved`/`approved_jump` (already-vetted or pre-approved, e.g. Stripe-paid) count toward the
watermark now — a flood of no-show pending requests literally cannot trip auto-stop, no IP
tracking needed. Fixed in all 6 places that computed this count: relay `static/admin.html`'s
`S.outstandingCount`, both LAN `admin.html` files' outstanding-count fetch, iOS
`AppState.evaluateAutoManageRequests()` + `AdminView`'s live count caption, Android
`MainActivity.evaluateAutoManage()` — plus every "Outstanding = …" caption across all 13 surfaces
reworded to state the exclusion explicitly, not just silently changed. All three repos
rebuilt/re-verified clean after the change and re-pushed.

**Per-requester outstanding-request throttle, added 2026-08-28, same session — a separate,**
**complementary anti-abuse mechanism, not a replacement for the auto-manage narrowing above.**
Follow-up scenario the user raised: even with auto-manage's watermark now immune to a pending-
request flood, one anonymous, unauthenticated guest could still spam the free/pay-to-bartender
path purely to clutter the admin/bartender Requests screen (no cost to them, no auto-stop
triggered since it's all `pending`). User first asked whether per-IP throttling is viable over
WiFi/Hotspot — confirmed yes: each device gets its own DHCP-assigned local IP in both LAN modes
(same assumption `_bartender_lockouts` already relies on), but internet/relay-mode customers can
share a public IP behind carrier CGNAT/café NAT, so IP alone risks false-positives there — then
proposed combining IP with `customer_id` (the browser-persisted localStorage id every request
already carries, previously used only for admin.html's truncated device-label display). Landed as
a **union of the two signals**, not their intersection: a new request is rejected
(`MAX_OUTSTANDING_REQUESTS_PER_REQUESTER = 2`, i.e. blocked once the requester already has 3+
outstanding) if the count of the requester's own not-yet-played/denied/unfulfilled requests
matching **either** the same source IP **or** the same `customer_id` already exceeds the limit —
matching on just one signal alone doesn't let an abuser reset their count, they'd need to evade
both simultaneously (new IP *and* cleared browser storage). Deliberately the broader outstanding
definition (pending **+** approved/up-next, not the auto-manage narrowing above) — the whole point
here is stopping pending-request spam specifically, unlike auto-manage's counter which exists to
protect the venue's overall pipeline capacity.

Implemented on the three surfaces that actually receive an anonymous request over the network —
**not** kiosk-native (no meaningful "requester IP" for a walk-up guest at the physical device, and
trivially self-limiting since a human bartender/staff is right there) and **not**
create-payment-intent/payment-confirmed (a completed Stripe charge is real money changing hands,
already self-limiting, and blocking post-charge would be customer-hostile):
- **Relay** (`main.py`): new `SongRequest.requester_ip` field (captured via `request.client.host`,
  same idiom `bar_authenticate()`'s `client_ip` already established), new `_requester_outstanding_
  count()` helper, checked in `bar_request()` only — returns `429` with a clear message on
  rejection. Smoke-tested directly against a local server (3 requests from the same customer_id
  succeed, the 4th correctly 429s; a different customer_id sharing the same source IP also
  correctly collides — confirms the union-match works as designed, not just asserted).
- **iOS** (`LocalServer.swift`'s `/api/request`): new `SongRequest.requesterIp` field (`String? =
  nil` — must have an explicit default for Swift's synthesized memberwise init to treat it as
  omittable, easy to get wrong), captured via `req.address` (Swifter, already used elsewhere for
  hotspot/wifi IP-range detection), same union-count check inline in the handler.
- **Android** (`LocalServer.kt`'s `handleSubmitRequest`): new `LocalRequest.requesterIp` field,
  captured via `session.remoteIpAddress` (NanoHTTPD, already used for bartender-pairing IP
  recording), same union-count check. Incidentally found and fixed a real latent gap while wiring
  this up: `jsonError()`'s status-code `when` block had no `429` case (would have silently
  returned `500` instead) — same class of bug as the pre-existing 401 gap documented earlier in
  this file, fixed the same way.
- **All three `customer.html` copies** (`static/`, `WebApps/`, `assets/`) gained a `res.status ===
  429` branch in `submitRequest()`/`reqSubmit()`, surfaced via a plain `alert()` (not the heavier
  full-screen `showError()` state the 403/404 cases use) — deliberately lighter-weight, since this
  isn't a dead-end: the guest's basket selection is still valid, they just need to wait for an
  existing request to resolve, unlike a truly expired/offline session.

All three repos rebuilt clean (`xcodebuild ... build`, `:app:compileDebugKotlin` +
`:app:assembleDebug`) and pushed per the standing cross-repo policy.

**Per-requester throttle refined to count PENDING SONGS only, same day, after user walked through**
**a concrete example.** User's scenario: guest X requests 3 songs, they get approved and queued to
play — X should still be able to request 3 more (since 0 songs are actually awaiting review at
that point); only after that second batch is submitted (bringing the to-be-approved count to 3)
should X be barred, and only until that count drops back below 3. Explicitly: "already approved
(and paid) requests do not count here" — a fundamentally different rule than what shipped a few
hours earlier, which counted `pending + approved/approved_jump` together (mirroring auto-manage's
*original*, pre-narrowing outstanding definition — the two features had drifted back toward the
same bug auto-manage itself was fixed for earlier the same day). Reworked all three
implementations: counts **individual songs** (not request objects — a 3-song bundle is common via
the `price_for_three` feature, and the user's own example is phrased in songs, not requests) in
requests that are `status == "pending"` **and** `payment_method != "stripe"` (a Stripe item can
transiently read raw-status "pending" too, before the host's own echo confirms it — see
`bartender_requests()`'s docstring on relay-side pending/approved display timing — but it was
never headed for bartender review, so it must never count). `MAX_OUTSTANDING_REQUESTS_PER_REQUESTER`
renamed to `MAX_PENDING_SONGS_PER_REQUESTER` (relay), `_requester_outstanding_count()` renamed to
`_requester_pending_song_count()`; iOS/Android equivalents renamed/reworked the same way. The
union-of-IP-and-customer_id identity design from the initial cut is unchanged — only what counts
as "outstanding" changed.

**Caught a real relay-side timing subtlety while re-testing, not a bug — the architecture working**
**as designed.** A first manual smoke test (bartender-approve a bundle via `bar_authenticate()` +
`bartender_approve()`, immediately resubmit) showed the second submission still blocked — traced
to `bartender_approve()`'s own docstring: "Don't mark approved here — host confirms via up_next on
next sync." The relay's raw `bar.requests[rid].status` genuinely stays `"pending"` until a real
connected host's next 5s sync echoes back the confirmed status (same async window
`bartender_requests()`'s display-status override exists to paper over for the *admin's own view*,
which this new count doesn't get to use since it needs the raw stored status, not a display
computation). Re-tested with a simulated `/api/host/sync` call carrying the confirmed status in
between the two submissions — behaved exactly as designed (2nd bundle succeeds, 3rd blocked at
3 pending songs). **iOS and Android don't have this lag at all** — confirmed by reading both
platforms' local bartender-approve handlers directly: `LocalServer.swift`'s `/api/request/approve`
and `LocalRequestManager.kt`'s `approveRequest()` both flip `status` to `.approved`/`APPROVED`
synchronously, in the same call, since LAN mode has no separate host to wait for.

**Auto-manage number fields: select-all-on-focus + visible-as-an-input styling, all 7 places**
**(2026-08-28).** User: "hard to see that the numbers are actually input fields; also hard to
select the number to delete it" — traced the visibility complaint to real low-contrast CSS/colors
(the HTML surfaces' `.am-num-field` used a background essentially the same shade as its own
container card, with a near-invisible border), fixed on all three `admin.html` copies with a
lighter fill + more visible border, plus `onfocus="this.select()"` so a tap highlights the
existing digits (JS's own native behavior, no extra plumbing needed). Android's Compose fields
needed converting from a plain `String` state to `TextFieldValue` + `Modifier.onFocusChanged {
if (it.isFocused) field = field.copy(selection = TextRange(0, field.text.length)) }`, done
identically on both `AdminScreen.kt` and `AutoManageStep.kt`, plus the same lighter-fill/
more-visible-border treatment via `OutlinedTextFieldDefaults.colors()`'s container/border params.
**iOS needed real UIKit bridging** — SwiftUI's native `TextField` has no select-all-on-focus
capability at all (tapping in only places a cursor); new `SelectAllTextField.swift` wraps a plain
`UITextField` (`UIViewRepresentable`, first new Swift file added this session — confirmed the
project uses Xcode's synchronized-file-groups feature so it needed no manual `.pbxproj` edit,
picked up automatically by a rebuild) with `textFieldDidBeginEditing` calling `selectAll(nil)`,
matching env `isEnabled` for the Requests card's disabled-in-Manual-mode state, and its own
`inputAccessoryView` Done button — a bridged `UITextField` isn't tracked by SwiftUI's
`.focused()`/`.toolbar(.keyboard)` mechanism the way a native field is, and `.numberPad` has no
Return key, so the existing shared `autoManageFieldFocused` `@FocusState` (no longer needed once
these two fields stopped being native `TextField`s) was removed, along with a direct
`UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder)...)` call replacing
its one remaining use (dismissing the keyboard programmatically after `saveAutoManageThresholds()`
completes). Used on `AdminView.swift`'s two live fields and `SetupView.swift`'s wizard-step
equivalents — 4 call sites, one shared component. All three repos rebuilt clean.

**Mode selector redesigned as a real either/or knob, controls scoped to only the active mode, all**
**7 places (2026-08-28).** User: the button-pair (Manually/Automatically, both always visible)
"is more like seeing options" than conveying an either/or setting, and wanted "a knob that they
either turn toward Manually or toward Automatically" — plus, once a mode is picked, only the
controls for *that* mode should render at all, not the other mode's controls faded/disabled
underneath. Replaced the two-button row with each surface's own existing single-switch component
(reused, not reinvented) on every surface: render/LAN `admin.html`'s `.toggle-track`/`.lan-toggle`
(already used for the Stripe/Bartender/Accepting-requests toggles elsewhere on the same card),
iOS's `Toggle` (`AdminView.swift` + `SetupView.swift`'s wizard step), Android's `Switch`
(`AdminScreen.kt` + `AutoManageStep.kt`) — the switch's own label text flips between "Manually"/
"Automatically" as it's dragged. Below it, `if (autoManage) { auto-only controls } else {
Accepting-requests toggle + its "Start/Stop on the Admin screens" note }` — the inactive mode's
block doesn't render in the DOM/tree at all on any surface, not just `display:none`/`.disabled`.
The wizard step (`AutoManageStep.kt`/`SetupView.swift`'s `autoManageStep`) already conditionally
showed only the relevant controls before this pass (it never had an Accepting-requests toggle to
begin with) — only needed the same knob-visual swap for consistency with the other 5 surfaces,
no structural change there. Dead `.mode-switch`/`.mode-btn` CSS removed from all three
`admin.html` copies. All three repos rebuilt clean.

**Knob redesigned again, same day: a plain on/off switch still implied "off" meant "disabled,"**
**even after the button-pair fix above.** User: "when it is on Manually, the knob infers that I
should turn it on TO BE manually" — a real, distinct critique from the earlier one: a segmented
button-pair reads as "here are your options" (fixed by the previous redesign), but a boolean
switch/toggle carries its *own* baked-in semantic of "on = enabled, off = disabled" regardless of
what label sits next to it, which doesn't fit two equally-valid named positions where neither is
inherently the "off" one. Proposed fix, followed exactly: show both labels ("Manually" /
"Automatically") above a single wide, fat slide control, with the control itself conveying
"wherever you turn it, will be the setting" rather than "off vs on." Replaced the native
`Toggle`/`Switch` on every surface with a custom two-position slide knob: a wide track (`height:
34px` HTML, matching on both platforms) whose thumb fills exactly half the track and sits on
whichever side (left/right) is currently active — position, not color-as-enabled, carries the
meaning. New shared components, one per platform, used at all 4 native/wizard call sites:
`JukeBar/ModeKnob.swift` (SwiftUI, plain `HStack`+`Spacer` trick to push a colored
`RoundedRectangle` to either half, no `GeometryReader` needed) and
`ui/ModeKnob.kt` (Compose, `Modifier.fillMaxWidth(0.5f)` + `Alignment.CenterStart`/`CenterEnd`).
HTML surfaces reuse the identical `.am-knob-track`/`.am-knob-thumb`/`.am-knob-labels` CSS pattern
(absolutely-positioned thumb, `transform: translateX(100%)` when "on") on all three `admin.html`
copies — dead `.mode-row` CSS removed where it became unused. All three repos rebuilt clean.

## Planned next
- Song counts from iOS/Android on register: `artists: [{name, song_count}]` instead of `[String]` — improves pie chart accuracy
- Stripe live key: apply under own business account to validate payment flow end-to-end before bar rollout
- Apple Pay domain file is served; needs Stripe dashboard domain registration to activate
- LLM fallback for obscure artists (no Last.fm data): deferred — needs API cost/rate infrastructure first
- Recommended playlist pipeline: deferred (see `docs/render_spec.md`)
