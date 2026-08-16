# JukeBar — Full System Test Plan

Covers both host platforms (iOS `~/dev/giffy/JukeBar`, Android `~/dev/giffy/spotonjukebar`) and the
relay (`jukebarweb`). Checkboxes are per-test; tag each run with the platform(s) it applies to.

**Conventions**
- `[Both]` = run once per platform (iOS and Android), unless noted otherwise.
- WiFi and Hotspot are tested as **one** case each below — same functional surface, different network setup.
- Surface names match the terms you use: **render** (relay/internet) customer/admin/bartender;
  **wifi/hotspot** customer/admin/bartender; **kiosk** admin/customer (on-device, native). Bartender has
  no kiosk-native surface — 3 surfaces only, not 5.
- Items marked **(regression)** were real bugs found and fixed this session (2026-07) — keep these in
  the permanent suite, don't drop them after one clean pass.

---

## Surface Reference Matrix

| Page type     | iOS kiosk-native | Android kiosk-native                | WiFi/Hotspot LAN            | Render (internet)               |
|---------------|-------------------|--------------------------------------|------------------------------|----------------------------------|
| Admin         | `AdminView.swift` | `AdminScreen.kt`                     | `admin.html` (per platform)  | `static/admin.html` (shared)     |
| Bartender     | — (none)          | — (none)                             | `bartender.html` (per platform) | `static/bartender.html` (shared) |
| Customer      | `KioskView.swift` | `KioskView.kt` + `LocalRequestSheet.kt` | `customer.html` (per platform) | `static/customer.html` (shared) |
| Homepage      | —                 | —                                     | —                             | `static/index.html` (`/`)        |
| Discover map  | —                 | —                                     | —                             | `static/discover.html` (`/discover`) |
| iOS docs      | —                 | —                                     | —                             | `static/ios.html` (`/ios`)       |
| Android docs  | —                 | —                                     | —                             | `static/android.html` (`/android`) |

Homepage/Discover/docs pages are community surfaces, not per-bar-session pages — no kiosk-native or
LAN equivalent exists or is expected for them; internet-only by nature, listed here for completeness
against the rest of the matrix, not because they're missing elsewhere.

---

## A. End-to-End Cross-Surface Scenarios

**Read this section differently from the rest of the document.** Sections 1–14 are an inventory —
does each surface, in isolation, do what it's supposed to. That can all pass while the *system* is
still broken, because almost every real feature here works by one surface changing host state and
that state broadcasting out to every other surface (`CLAUDE.md`'s "governing principle"). A scenario
below is a single story with multiple actors and a time dimension — trace it start to finish; if any
step doesn't match, that's the bug, and the step number tells you which link in the chain broke, not
just "something's wrong." Steps within a scenario are ordered and each depends on the previous one.

**Transport reminder for every scenario below**: a bar session is on wifi, hotspot, *or* internet —
never more than one at a time — and render (relay) surfaces only exist at all for an internet-transport
session (`hostRegisterOnRelay()` is internet-only). **kiosk-native admin/customer is the one surface
that's reachable regardless of transport** (same on-device process as the host, no network involved),
so it's the only surface that can validly appear alongside *either* branch. Where a scenario is worth
running on both transports, it says so explicitly with two branches — don't mix wifi/hotspot and render
surfaces into one "simultaneous" checklist for a single session.

**Platform reminder, same idea, different dimension**: "the host" in every scenario below is either the
iOS app or the Android app — never both at once, and their settings-propagation/request-lifecycle/
effective-value logic is implemented **independently** (`AppState.swift`+`LocalServer.swift` vs
`MainActivity.kt`+`RelayService.kt`+`LocalServer.kt`), so a pass on one platform is not evidence the
other works — this session alone found several real platform-specific divergences (Android's PIN
plaintext-vs-hash gap, Android's `jsonError()` missing status branches). **Unless a scenario says
otherwise, run it once per host platform, as two independent passes** — same convention as the
`[Both]` tag used throughout sections 1–15, just not repeated inline here for every scenario. A2 is
platform-specific by construction (the bug it regression-tests lived in Android's `RelayService.kt`
only — see its own note); A7 is Android-only because iOS has no Spotify-outage-equivalent code path
at all (confirmed architectural, not unverified — see testing.md §8 and CLAUDE.md).

### A1. Settings toggle propagates to every surface, with correct lock/unlock timing

Proves: `desired_settings` single-slot propagation (`CLAUDE.md` — settings section).

**Setup** (both branches): host platform per the reminder above (run once per platform) — Local+Remote
kiosk mode (need both kiosk-native and a remote admin/customer surface reachable) — Stripe **ON**,
Bartender Pay ON (Stripe is the toggle target; Bartender ON just keeps approval-required throughout so
the scenario isn't accidentally testing free/auto-accept mode too) — any catalog, actively playing
(so `accepting_requests` stays effective and the request-flow checks aren't confounded by a paused bar).

**Branch 1 — internet transport:**
- [ ] Start with Stripe **ON**. On **render admin**, toggle Stripe OFF.
- [ ] Immediately after clicking: the Stripe control on **render admin itself** shows dimmed/locked
      (`settings_pending`) — it does not just silently flip.
- [ ] Within one host sync cycle (~5s): host applies the change locally.
- [ ] **kiosk admin** (native) now shows Stripe OFF, unlocked.
- [ ] **render admin**'s own control unlocks and confirms OFF once the host's echo comes back.
- [ ] **render bartender**: payment-method display for *new* incoming requests reflects the change (no
      more Stripe option surfaced to customers).
- [ ] **render customer** and **kiosk customer**: Stripe payment option is gone from the request flow
      on both, not just one.
- [ ] Toggle it back ON from a *different* surface this time — **kiosk admin** — and confirm the same
      full propagation happens in reverse, proving it's not a one-directional fluke.

**Branch 2 — wifi/hotspot transport (repeat independently, separate session):**
- [ ] Same sequence, substituting **wifi/hotspot admin/bartender/customer** for the render surfaces.
      This path applies directly to host state with no relay round-trip at all — confirm the unlock is
      correspondingly near-instant, not lagging by a sync-cycle's worth of latency the way the
      internet-transport branch legitimately does.

### A2. A request born on the kiosk is cancelled from Render admin — actually stops playing everywhere

Proves: host-is-source-of-truth request lifecycle + the kiosk-origin Cancel bug fixed 2026-07
(`RelayService.kt`'s `injectedRequestIds` fallback) — **regression test, keep permanently.** This
scenario is internet-transport-specific by nature — the bug it guards against only existed in the
relay-mediated action path (`RelayService`'s action queue); wifi/hotspot Cancel applies directly to
host state via `LocalServer`, a different code path entirely that never had this bug.

**Platform note**: the specific bug this regression-tests was Android-only (`RelayService.kt`'s
action-queue handling) — if only one platform can be run, prioritize Android. But the general
capability (Cancel pulling a song out of the live playback queue) exists on iOS too via
`MusicService.cancelRequest()`, so run this on iOS as well when possible rather than assuming a
clean Android pass covers it.

**Setup**: free/auto-accept mode (Stripe OFF, Bartender Pay OFF — the request needs to auto-approve
with no human step, since the scenario starts from an already-live Up Next entry) — Local+Remote or
Local Only kiosk mode (either works, since the request originates from the kiosk's own local button
either way, not a web surface) — **internet transport** (structural requirement, not a choice — this
is the one bug that only existed in the relay-mediated path) — any catalog, actively playing.

- [ ] Submit a request via the **kiosk's own local Request button** (not a web surface).
- [ ] Confirm it auto-approves and appears in Up Next on: **kiosk itself**, **render customer**,
      **render admin**.
- [ ] From **render admin** (over the internet, physically nowhere near the kiosk), click Cancel on
      that request.
- [ ] Confirm the song is actually pulled out of the *live playback queue* — it does not play when its
      turn comes, not just marked denied in a list somewhere.
- [ ] Confirm it disappears from Up Next on **every** surface simultaneously within one sync cycle:
      kiosk and render customer — not just render admin's own view.

### A3. Stripe payment on Render customer reaches the kiosk queue and plays, then shows correctly everywhere as played

Proves: the full Stripe → `new_requests` upstream → host adoption → live queue → played-detection →
`bar.requests` downstream loop, across the whole surface set.

**Setup**: host platform per the reminder above — **internet transport** (structural requirement:
Stripe payment only exists via render customer, `bar_create_payment_intent`/`bar_payment_confirmed`
are internet-only endpoints) — kiosk mode Local+Remote or Remote Only (**not** Local Only — its
customer page 404s, see A4 — the whole scenario depends on reaching render customer) — Stripe
**ON** with a real **test** secret key configured (a live key isn't needed, but a placeholder/empty
one is — Stripe calls will fail before the scenario even starts) — catalog must include at least one
track whose id you'll submit, actively playing.

- [ ] Submit and pay for a request via **render customer** page (Stripe test key + card, or Apple
      Pay/Google Pay per §4).
- [ ] Request appears with a 💳 badge in Up Next on **render admin** and **render bartender** — no
      approve/deny buttons shown for it (Stripe skips review).
- [ ] It also appears (unlabeled/generic) in the **kiosk's own** Up Next preview.
- [ ] Let it actually play through.
- [ ] Confirm it's pruned from Up Next **immediately** on kiosk, render customer, and admin — not lagging
      on any one surface (2026-07 regression: kiosk/render-customer used to lag behind admin here).
- [ ] Confirm it now appears in **Reports/Past Requests** on render admin with the 💳 badge and the
      price actually charged (not today's live price if pricing changed since).

### A4. Local Only — full walkthrough: setup through request display, map visibility, and lockout scope

Proves every Local-Only-specific behavior as one coherent operator story rather than scattered checks:
wizard-time Stripe disable, QR suppression, kiosk request name requirement, Up Next strip vs popup
content, the `checkCustomerAllowed()` vs `checkLocalMode()` lockout-scope split (a real historical bug:
the first version of this lockout broke *all* LAN admin/bartender routes, not just the 4 customer
ones), and map registration continuing to work despite the customer lockout. The setup/kiosk/map parts
below are transport-independent (on-device or connectivity-only, not gated by which transport is
chosen) — only the lockout-scope part genuinely needs both branches run for real, since LAN and relay
enforce it via two separate implementations.

**Setup**: host platform per the reminder above (the wizard/kiosk-lockout code is per-platform, so
this genuinely needs both passes, not just the lockout-scope branches). Payment mode and catalog are
established by the setup steps themselves below, not a precondition — this scenario tests going
through setup, unlike most others which assume a bar is already live.

- [ ] During the setup wizard, select Local Only kiosk display mode with Stripe toggled ON.
- [ ] Wizard's Stripe step shows it visible but **disabled**, with the "no customer page exists to pay
      from" caption — not hidden, not silently editable.
- [ ] Complete setup with `listOnMap` ON.

**Post-setup, live, on the kiosk itself:**
- [ ] Kiosk shows **no QR code** anywhere on screen.
- [ ] Kiosk's local Request button is the only way to submit — confirm it's actually present and usable
      (Local Only ≠ fully locked, per the earlier `effective_stripe` distinction in A6).

**Kiosk request flow:**
- [ ] Attempt to submit a local request with a **blank requester name** — confirm submission is
      actually blocked/rejected, not just silently accepted as anonymous.
- [ ] Submit a valid free request with a name filled in.
- [ ] Once approved, the song appears in the kiosk's **always-visible Up Next preview strip**.
- [ ] Tap to expand the **full-queue popup overlay** — the same request appears here too, and **only
      here** shows the requester's name (the strip itself does not display names).
- [ ] Popup overlay self-dismisses after 15s of no interaction (existing kiosk-native invariant).

**Map visibility despite the customer lockout:**
- [ ] Confirm the bar **does** appear on `/discover` with its current playlist shown — the customer
      lockout is request/browse-specific; it does not suppress map registration, a separate, unrelated
      codepath (confirmed this session, §13).

**Lockout scope — run both branches for real, separate sessions:**

*Branch 1 — wifi/hotspot transport:*
- [ ] **wifi/hotspot customer** page: write endpoints (request, payment-intent, payment-confirmed,
      request status) return real 404/503 — not a friendly error page.
- [ ] **wifi/hotspot admin** can still list pending requests, approve, deny — full functionality.
- [ ] **wifi/hotspot bartender** can still pair, list, approve, deny — full functionality.

*Branch 2 — internet transport:*
- [ ] **render customer** page: same 404/503 behavior on the 4 customer-exclusive endpoints.
- [ ] **render admin** and **render bartender**: fully functional, unaffected.

- [ ] In both branches: only the kiosk's own local Request button can create new requests; everything
      else about admin/bartender operation is completely unaffected by the customer lockout.

### A5. `accepting_requests` OFF hides the ask everywhere, without touching in-flight requests

**Setup** (both branches): host platform per the reminder above — Local+Remote kiosk mode — payment
mode doesn't matter structurally (this gate is independent of Stripe/Bartender/free), but free/
auto-accept keeps the "get 1+ requests pending/approved" precondition below quick to reach — any
catalog, actively playing. Before toggling, get the bar into a state with **1+ requests already
pending or approved** (submit and, if not auto-accept, approve one) — this is a required precondition,
not just a nice-to-have, since the whole point of the scenario is confirming those are left alone.

**Branch 1 — internet transport:**
- [ ] With 1+ requests already pending/approved, toggle **accepting_requests OFF** from render admin.
- [ ] kiosk's local Request button hides/disables — kiosk still shows now-playing and QR (not blank).
- [ ] render customer: Request submission UI disabled/hidden.
- [ ] Already-pending/approved requests from before the toggle are **unaffected** — bartender can still
      approve/deny them, Up Next still plays them out normally.
- [ ] Toggle back ON — Request capability returns on kiosk and render customer simultaneously, no
      restart needed.

**Branch 2 — wifi/hotspot transport (repeat independently, separate session):** same sequence,
substituting wifi/hotspot admin/customer for the render surfaces.

### A6. `effective_stripe` — Local Only + Stripe ON + Bartender OFF behaves as free, without losing the raw preference

Proves the 2026-07-22 bug: this combination used to hide the kiosk's own Request button entirely
(read as "still needs approval" when it should read as free/auto-accept). The `effective_stripe`
computation itself doesn't depend on transport, so this only needs one run — pick either transport,
using kiosk admin plus whichever remote admin surface matches (render or wifi/hotspot).

**Setup**: host platform per the reminder above — Local Only kiosk mode specifically (this is a
Local-Only-only bug — `effective_stripe` is a no-op in the other two kiosk modes) — Stripe **ON**
(raw), Bartender Pay **OFF** — either transport, one run, per the note above — any catalog, actively
playing.

- [ ] Bar in Local Only kiosk mode, Stripe toggle **ON** (raw), Bartender Pay **OFF**.
- [ ] kiosk local Request button is **visible and usable** (not hidden) — this is the actual regression.
- [ ] A request submitted this way auto-approves with no payment step and no approval wait — behaves as
      pure free/auto-accept.
- [ ] Stripe toggle on **kiosk admin** and on the matching remote admin surface (render *or*
      wifi/hotspot, whichever the session is on) both still show it as **ON** (raw value) —
      dimmed/disabled with the "no customer page exists to pay from" caption, not silently flipped off.
- [ ] Switch kiosk mode to Local+Remote — Stripe becomes live again automatically, still ON, no
      re-toggling needed; confirms the raw value was preserved, not lost, while it was inert.

### A7. Spotify outage mid-session (Android) — kiosk locks out customers, but admin/bartender keep working, and paid state survives

Proves the 2026-07-30/31 outage-recovery feature end-to-end, including the surface split. Inherently an
internet-transport scenario — the interesting part is render admin staying in control while the kiosk
itself is locked, which only makes sense to test where render is actually the relevant remote surface.

**Setup**: **Android only** (see the platform reminder above — iOS has no equivalent code path) —
internet transport (structural) — **catalog must include real Spotify tracks, not a local-files-only
catalog** — there's nothing to fail if nothing in the queue is ever routed through Spotify at all —
Local+Remote or Local Only kiosk mode, either works — payment mode: mix of paid and free as the first
step below establishes, not a precondition on its own.

- [ ] Bar on internet transport. While Spotify is connected and stable, queue 2+ paid or
      free-approved requests into Up Next.
- [ ] Force/observe a Spotify outage (2 consecutive failures, any type/path — the design was
      consolidated to a single shared counter 2026-08-08, see CLAUDE.md/item 12; a single isolated
      failure now just marks+skips instead of escalating) — kiosk drops to the blocked "ask staff"
      screen; confirm **all** customer interaction is blocked, only the Staff button is live.
- [ ] While the kiosk is blocked: from **render admin/bartender**, confirm approve/deny/Cancel actions on
      *other* requests still work — the host's relay sync loop is not paused by the outage, only local
      playback is. (If this fails, that's a real gap: admin should not lose control just because
      Spotify did.)
- [ ] Enter the correct PIN on the kiosk's blocked screen (or use "Re-attach to Spotify" on AdminScreen
      as the alternate manual path).
- [ ] Confirm the specific song that failed resumes — not a different, wherever-the-queue-drifted song.
- [ ] Confirm both paid requests still show correctly in Up Next across kiosk, render customer, and
      render admin after recovery — nothing was silently lost during the outage.
- [ ] Confirm settings (every toggle) are byte-for-byte unchanged from before the outage — this path
      never touches the setup wizard.

### A8. Genre/map opt-in — LAN-only bar appears on the map without genre data, gains it only once on internet transport

Proves the `hostRegisterOnRelay`/`registerOnMap` split confirmed this session (§13's note).

**Setup**: host platform per the reminder above — wifi/hotspot transport to start (the scenario
switches to internet partway through, on the same bar) — device needs a genuine internet uplink even
on wifi/hotspot (map registration is always-on regardless of transport, but still needs real
connectivity to reach the relay) — **catalog should be a real, already-profiled playlist** (matching
an existing Last.fm genre profile), not a synthetic test catalog with made-up artist names — the
genre-coloring assertion is meaningless if there's no profile data to ever populate.

- [ ] Host on wifi/hotspot transport (device has a real internet uplink), `listOnMap` **ON**.
- [ ] Confirm the bar appears on `/discover` — name, location, playlist visible.
- [ ] Confirm it shows **no genre coloring** in its pie chart (expected — no relay `BarSession` exists
      for a LAN-only bar, so genre polling can't run).
- [ ] Switch the same bar to internet transport (new session/setup) with `listOnMap` still ON.
- [ ] Confirm genre coloring now populates on `/discover` within one profile-cache refresh cycle.

### A9. Turning off both Stripe and Bartender Pay transitions a live bar to free/auto-accept

Proves the `require_approval`/`effective_stripe` computation genuinely re-evaluates and takes effect
live, mid-session, not just at setup time — and that it's the combination of *both* being off (not
either alone) that flips the mode.

**Setup**: host platform per the reminder above — `require_approval` doesn't depend on transport
(same reasoning as A6's `effective_stripe`), so one run on either transport is sufficient — Local+
Remote kiosk mode (not Local Only, or Stripe would already read as inert regardless of its raw
toggle — that's A6's scenario, don't conflate them) — any catalog, actively playing.

- [ ] Start with Stripe **ON** and Bartender Pay **ON** (approval required either way).
- [ ] From any admin surface, turn Stripe **OFF** alone.
- [ ] Confirm approval is **still required** — Bartender Pay alone still gates it; a new request still
      needs a bartender tap, doesn't auto-approve.
- [ ] Now also turn Bartender Pay **OFF** (both off).
- [ ] Submit a **new** request after this point — confirm it auto-approves with no approval step and no
      payment step, behaving as pure free/auto-accept.
- [ ] Confirm this took effect **live**, mid-session, without End Session or any restart.
- [ ] Turn either one back ON — confirm the *next* new request goes back to requiring approval/payment;
      requests already auto-approved during the free window are unaffected retroactively.

---

## 1. Setup & Onboarding `[Both]`

- [ ] Fresh setup wizard, start to finish, all steps in order
  - [ ok] Kiosk Display Mode step (Local Only / Local+Remote / Remote Only)
  - [ ] Network/Transport step — a transport is **always** chosen here regardless of display mode
  - [ ] Local content/folder step (Android) / equivalent library step (iOS)
  - [ ] Spotify (Android) / Apple Music (iOS) device pairing step
  - [ ] Playlist selection step
  - [ ] Bar name entry
  - [ ] Admin PIN entry (4–6 digits)
  - [ ] Approval mode step (Free / Bartender Pay / Stripe)
  - [ ] Pricing step (per-song, 3-song bundle, currency)
  - [ ] Summary/confirm step, session actually starts
- [ ] **End Session — kiosk-only, redesigned 2026-08-01.** Full wizard wipe, previous values
      pre-filled, Up Next intentionally does NOT survive it (a genuine restart — playlist/settings/
      everything can legitimately change). **Reachable only from the kiosk's own on-device admin
      screen on both platforms** — the equivalent "Stop/End Session" button and its backing endpoint
      (`/api/admin/stop` on LAN, the relay's `stop_session` action on render) were **removed entirely**
      from LAN admin.html (both platforms) and the shared render `static/admin.html`. Rationale: the
      wizard can only ever be completed at the kiosk regardless of where it's triggered from, so a
      remote trigger only exposes the admin wizard on the public-facing kiosk screen to bystanders
      without anyone physically present to supervise it — and a remote page would orphan its own
      session the moment the kiosk's QR/session rotates anyway, since it can't retrieve the new one.
  - [ ] Confirm the button/control is genuinely gone from wifi/hotspot admin AND render admin on both
        platforms — not just hidden, actually unreachable (no working endpoint left to call directly)
  - [ ] Confirm kiosk admin's End Session still works normally, full wizard wipe, on both platforms
  - [ ] Android: End Session keeps the Spotify login (no re-auth needed next setup) — this was already
        true before the redesign, `restartSession()`'s existing behavior, unchanged
  - [ ] iOS: Apple Music access is an OS-level authorization, not an app-managed token — there is
        nothing to "keep or clear" here at all; confirm no re-auth prompt appears after End Session
        (should already hold true, structurally, with no code change needed for this specifically)
- [ ] **(regression, iOS, fixed 2026-08-01) Pausing must never rotate the session/QR or wipe Up Next**
      — neither the manual admin play/pause toggle (already correct, calls `MusicService.pause()`
      directly) nor the automatic 30-minute idle timer (was calling `stopSession()`, which minted a
      new session id **and** wiped the live queue as a side effect of merely going idle — fixed by
      renaming to `stopPlaying()` and stripping those side effects). Test: let a session sit idle
      past 30 minutes with 1+ approved requests in Up Next — confirm the QR code is unchanged and
      Up Next is intact when it auto-pauses.
- [ ] Re-pairing host device to IDE for deployment doesn't require a fresh setup (sanity, not a product test)
- [ ] **(Android, gap found by code audit 2026-08-01)** Skip **both** the Spotify device step and the
      local folder step in the same setup run — confirm the resulting empty queue doesn't crash
      playback (`PlaybackCoordinator.play()` guards against a null `currentSong`), and note there's
      currently **no operator-facing indication** that the queue is empty at all — decide if that's
      acceptable or needs a visible warning

## 2. Transport Modes (WiFi and Hotspot = one test case each)

- [ ] WiFi/Hotspot: `LocalServer` starts, correct LAN IP shown, QR encodes that IP
  - [ ] wifi/hotspot customer page reachable and functional
  - [ ] wifi/hotspot admin page reachable and functional
  - [ ] wifi/hotspot bartender page reachable and functional
  - [ ] kiosk admin reachable (long-press → PIN) while on this transport
  - [ ] kiosk customer (Request button / now-playing / QR) functional
- [ ] Internet (Render): host registers with relay at startup, full catalog + bar details sent
  - [ ] 5s sync heartbeat keeps session alive; session survives a relay restart from the *host's* side
        (re-registers cleanly) — note: relay restart wipes in-memory `bar.requests`, that's expected,
        not a bug (see §13.1)
  - [ ] render customer reachable via `jukebars.com/bar/{id}?s={session}` URL, functional
  - [ ] render admin reachable, functional
  - [ ] render bartender reachable, functional
  - [ ] kiosk admin/customer still fully functional locally while on internet transport

## 3. Kiosk Display Mode (orthogonal to transport — test against both WiFi/Hotspot and Internet)

- [ ] **Local Only**
  - [ ] No customer-facing QR shown anywhere on kiosk
  - [ ] Local Request button is the only way to submit — present and working
  - [ ] wifi/hotspot customer page: write endpoints (request, payment-intent, payment-confirmed,
        request status) return real 404/503, not a friendly error page
  - [ ] render customer page: same 404/503 behavior for the internet-transport case
  - [ ] Stripe toggle **visible but disabled**, with explanatory caption, in both setup wizard and live
        Admin screen
  - [ ] `effective_stripe_enabled` reads false regardless of the raw stored toggle value — kiosk Request
        button visibility and auto-approve logic both reflect this correctly
  - [ ] Switching back out of Local Only later restores the operator's original Stripe preference
        (raw value was never mutated)
- [ ] **Local And Remote**
  - [ ] QR + remote customer surface works
  - [ ] Local Request button also works simultaneously
- [ ] **Remote Only**
  - [ ] No local Request button on kiosk
  - [ ] QR/remote customer is the only entry point
  - [ ] Kiosk still shows now-playing/QR (not blank)

## 4. Payment / Approval Modes

- [ ] Free / auto-accept: request needs no approval tap, plays automatically
- [ ] Pay to Bartender: request sits pending until bartender taps Approve; `payment_method="bartender"`,
      `paid=true` set at that moment
- [ ] Stripe
  - [ ] Card payment completes, request auto-injects via `new_requests`, no approve tap needed
  - [ ] **Apple Pay (iOS)** — confirmed reachable with the Stripe **test** key; completes as a success in
        the system without an actual charge. Good repeatable manual test path — re-verify after any
        Stripe-related change. *Do not use a live key for this test.*
  - [ ] Google Pay (Android) — equivalent check
  - [ ] Apple Pay domain file served correctly (`/.well-known/apple-developer-merchantid-domain-association`
        or equivalent) — full end-to-end Apple Pay activation is still blocked on Stripe dashboard domain
        registration per project notes; confirm current status before assuming this is fully live
  - [ ] **Correction (code audit 2026-08-01): `STRIPE_MINIMUMS` is NOT actually enforced anywhere.**
        Android's pricing step shows it only as an informational caption — `pricesValid` never checks
        against it, so the wizard lets an operator save a price below Stripe's floor. The relay's
        `create-payment-intent` has no server-side floor check either. Set a price below the minimum
        for the chosen currency and confirm what a customer actually sees when they try to pay: today
        that's Stripe's raw unformatted error text surfacing on `customer.html`, not a friendly message
        — and it's only discoverable at payment time, not caught at setup. Decide if this needs a real
        validation fix (wizard-side floor check, or a friendlier customer-facing error) rather than
        treating "enforced" as already true.
- [ ] `effective_stripe`: Stripe ON + Bartender OFF + Local Only kiosk mode → system behaves as free/auto-accept
      (kiosk Request button visible, no approval required) — this was a real bug, keep as regression check

## 5. Settings Propagation (`desired_settings` single-slot mechanism)

For **each** of the three toggles — `stripe_enabled`, `bartender_enabled`, `accepting_requests` — repeat:

- [ ] Toggle from render admin → host picks it up within one sync cycle, applies locally
- [ ] Toggle from wifi/hotspot admin (iOS) → applies
- [ ] Toggle from wifi/hotspot admin (Android) → applies
- [ ] Toggle from kiosk-native admin (iOS) → applies
- [ ] Toggle from kiosk-native admin (Android) → applies
- [ ] While a toggle is in-flight, the control shows **dimmed/locked** on every surface (`settings_pending`)
- [ ] Once host's echo matches, the lock clears and the new value shows consistently everywhere:
      render admin, wifi/hotspot admin, kiosk admin, bartender pages, kiosk Request-button visibility

## 6. Request Lifecycle (origin × surface × payment mode)

Origins to test: **kiosk-native**, **LAN web** (wifi/hotspot customer), **render customer**, **Stripe**
(card and Apple/Google Pay).

- [ ] For each origin: request appears correctly and promptly on —
  - [ ] admin Requests tab (pending) — kiosk, wifi/hotspot, render
  - [ ] admin Up Next after approval — all three
  - [ ] admin Reports/Past Requests after played — all three
  - [ ] bartender pending list — wifi/hotspot, render (no kiosk-native bartender)
  - [ ] customer-facing Up Next — kiosk strip, wifi/hotspot customer, render customer
- [ ] Approve action from admin — every surface, every origin
- [ ] Deny action from admin — every surface, every origin
- [ ] Approve/Deny from bartender — wifi/hotspot and render
- [ ] **Cancel (free requests only)**
  - [ ] Free request cancellable both pre- and post-approval
  - [ ] Stripe-paid request: Cancel blocked (403) even in the brief pre-confirmation `pending` window
  - [ ] Bartender-paid request: Cancel blocked (403) once `payment_method` flips to `"bartender"` at approval
  - [ ] **(regression)** Cancel on a **kiosk-originated** free request actually removes the song from the
        *live* playback queue on Android, not just from the stored request list — this was broken
        (silently no-op'd) and fixed 2026-07; re-verify after any `RelayService`/`injectSongs` changes
- [ ] Multi-song request / 3-song bundle pricing computed and charged correctly
- [ ] `SongRequest.price` frozen at creation — change the bar's live pricing *after* a request exists,
      confirm the request's Reports-tab price is unchanged (reflects what was actually charged)
- [ ] Payment method badges render correctly on admin/bartender: 💳 Stripe, 💵 bartender, muted "Free" badge
- [ ] **(gap found by code audit 2026-08-01)** Multi-song request (2–3 songs): deny or Cancel it
      **after** song 1 has already played but before the remaining song(s) have. `markPlayed()` only
      flips the request's overall status once the *last* song plays (both platforms), so at this point
      it's still `APPROVED` — confirm current behavior: the whole request flips to `DENIED`, and the
      Reports CSV/Past-Requests row shows it as a flat denied entry for the whole request, even though
      the customer already received 1+ songs. Decide whether this mislabeling (no "partially fulfilled"
      status exists) is acceptable as-is or needs a fix — currently it's just how the code behaves, not
      something anyone decided was correct.

## 7. Up Next / Playback Queue Correctness (Android — regression tests from 2026-07)

- [ ] A request's song is pruned from Up Next **immediately** when it starts playing — not just on a
      full queue rebuild or explicit Cancel
- [ ] After a song plays, admin (Requests/Reports), kiosk's own Up Next strip, and render customer page
      **all agree** — no surface lags behind showing it as still-upcoming
- [ ] Force a queue reshuffle (let the shuffle exhaust and wrap) — confirm an already-played request does
      **not** get resurrected into Up Next by the reshuffle
- [ ] Hitting Prev repeatedly — confirm current accepted behavior: previously-played request songs
      *do* reappear in Up Next as you step back (this is an accepted design tradeoff, not a bug — see
      2026-07 discussion; only flag if behavior has changed)
- [ ] "Requests" vs "Up Next" computed display status: a Stripe-paid or pure-auto-accept-mode request
      never shows in the pending/needs-review section, appears straight in Up Next
- [ ] **(new 2026-08-08, item 13)** Approve a request, let the queue exhaust and reshuffle while that
      request is still sitting unplayed in Up Next — confirm the request stays exactly where it was
      (not moved/delayed), while everything else around it gets freshly shuffled. With 2+ pending
      requests at reshuffle time, confirm they keep their existing relative order rather than getting
      scattered relative to each other. With zero pending requests at reshuffle time, confirm it's a
      plain full shuffle exactly as before — no behavior change. Note: the fix does **not** prevent a
      requested song from also coincidentally appearing again soon after in the freshly-shuffled filler
      portion — that near-term-duplicate case was deliberately left unhandled (considered and rejected
      as not worth the complexity/edge-case risk); only flag it if it looks like more than occasional
      shuffle luck.

## 8. Spotify Connectivity & Outage Recovery (Android, new 2026-07)

- [ ] Single transient "no Spotify device" blip (consecutive failures = 1): per-song 30s cooldown skip
      fires, playback falls back to local filler — confirm this is graceful, no crash, no error shown.
      **Known gap**: this path is still request-blind (a paid/approved song can be silently skipped by a
      single blip) — not yet hardened, tracked as follow-up
- [ ] 3 consecutive "no device" failures: breaker trips
  - [ ] Kiosk drops to the blocked "Temporarily Unavailable / ask staff" screen
  - [ ] **All** customer interaction is blocked — only the single Staff button is live
  - [ ] Staff button → PIN entry (reuses admin PIN) → correct PIN triggers reconnect
  - [ ] Reconnect: Spotify app launches briefly via Intent, then the specific failed song is retried
        (not wherever the queue happened to drift to)
  - [ ] Settings and Up Next are **fully intact** after recovery — nothing routes through the setup
        wizard or `LocalRequestManager.reset()`
- [ ] Manual "Re-attach to Spotify" button on AdminScreen
  - [ ] Works at any time, not just after a breaker trip
  - [ ] Resumes whatever's currently selected if no specific song was pending retry
- [ ] Long pause (multiple hours) then resume — confirm Spotify device lookup can fail on resume
      (same class of issue as backgrounding); "Re-attach to Spotify" resolves it
- [ ] **iOS: confirmed gap, not just unverified (code audit 2026-08-01).** Full read of `MusicService.swift`
      found no retry, reconnect, or outage-detection logic of any kind — `handleNowPlayingChanged()` only
      reacts to normal track-advance and cancellation, nothing for a playback failure or Apple Music auth
      revocation mid-session. This is a real platform gap, not parity-pending-verification. Test: force
      an Apple Music playback failure (e.g. revoke access mid-session) and confirm what actually happens
      today — likely just silent failure with no recovery path — then decide whether iOS needs equivalent
      treatment or whether Apple Music's playback has proven reliable enough in practice not to need it.

## 9. Cross-Platform Kiosk-Native Parity (iOS vs Android)

- [ ] `accepting_requests` gates the local Request button on **both** platforms (not just the web pages)
- [ ] `requesterName` is the field used by both platforms' local request flow (not a legacy `customerName`)
- [ ] Up Next preview strip (row count driven by screen height) present on both
- [ ] Tap-to-expand full-queue overlay present on both, **self-dismisses after 15s** of no interaction
      on both

## 10. UI Look-and-Feel Consistency (13-surface matrix)

- [ ] Admin: kiosk-native, wifi/hotspot, render — visually/functionally consistent across iOS and Android
- [ ] Bartender: wifi/hotspot, render — consistent across iOS and Android (no kiosk-native to compare)
- [ ] Customer: kiosk-native, wifi/hotspot, render — consistent across iOS and Android

## 11. Reports & History

- [ ] Report generation produces correct played counts
- [ ] Past Requests overlay (admin, on-demand fetch) shows played/denied history with correct badges,
      correct status text (not hardcoded "In queue" for historical rows)
- [ ] LAN admin's Played/Denied sections (always-fetched, client-grouped) match relay's on-demand overlay
- [ ] **(gaps found by code audit 2026-08-01, Android `LocalServer.generateReport()`)**
  - [ ] Tap "Generate Report Now" with **zero requests** in the session — confirm current behavior:
        silently no-ops (`if (requests.isEmpty()) return`), no file written, **no visible feedback** to
        the operator that nothing happened. Decide if that's acceptable or needs a shown message.
  - [ ] Tap "Generate Report Now" **mid-session**, while pending/approved (not yet played/denied)
        requests exist — confirm the report includes those rows with their current non-final status,
        not just finalized history.
  - [ ] Tap "Generate Report Now" **twice** in the same session — confirm two independent timestamped
        files are produced with overlapping data, no de-dup or cutoff marker between the two runs.
  - [ ] For a multi-song request's CSV row: confirm price is populated **only on the first song's row**,
        blank on subsequent songs (`idx == 0` check) — correct design intent, but a real gotcha if you
        validate report totals by naively summing the price column per row.

## 12. Bar Configuration Details

- [ ] Single currency field drives **both** bartender cash display and Stripe processing — no separate
      display-vs-processing currency drift
- [ ] Genre coloring: artist bubbles colored by genre; long-press shows raw Last.fm tags — both platforms
- [ ] QR code encodes the correct URL for the current transport + session, scans successfully

## 13. Community / Discover Map (`jukebars.com` — second product surface, separate from the relay)

This is a distinct feature area: jukebarweb also runs a public bar-discovery site, independent of any
single bar's session. **Both platforms have it, and both already do the right thing**: iOS
`registerOnMap()` (`AppState.swift:440`) and Android's call site in `MainActivity.kt` both register
whenever `listOnMap == true`, with **no transport gate** — connectivity is a runtime fact (the HTTP
call just succeeds or fails), not something hard-coded around. Confirmed by reading both
implementations directly — no fix needed, this was verified working as intended.

**Separate, correctly-scoped restriction, not a bug**: iOS's `startGenrePoll()` (`AppState.swift:563`,
populates the genre-color pie chart data on `/discover`) *is* gated to `internetMode` — but that's
because it hits a relay endpoint (`/api/bar/{id}/genres`) that only works if the bar has a `BarSession`
registered with the relay at all, and `hostRegisterOnRelay()` (`AppState.swift:488`) is itself correctly
`internetMode`-only (a wifi/hotspot-only bar's admin/bartender reach it via the LAN pages directly, no
relay session exists for it, the endpoint would just 404). Don't "fix" this gate — it's a real dependency.

- [ ] `/` (homepage, `static/index.html`) loads and renders correctly — check current state, this file
      has pending local edits as of this test plan being written
- [ ] `/discover` (`discover.html`) — bar list/map loads
- [ ] `/ios` and `/android` doc pages load
- [ ] **List on JukeBar Map toggle — both platforms**
  - [ ] Registers successfully on internet transport, both platforms
  - [ ] Also registers successfully on wifi/hotspot transport (as long as the device itself has an
        internet uplink) — both platforms, confirming the runtime-not-coded behavior actually holds
  - [ ] On wifi/hotspot with **no** internet uplink at all: registration call simply fails silently,
        rest of the bar's function (LAN customer/admin/bartender) is completely unaffected
  - [ ] Turning it **on** → `POST /api/map/register` fires, bar appears on `/discover`
  - [ ] Turning it **off** → `POST /api/map/unregister` fires, bar's entry (and all its playlist data)
        is fully wiped from the map, not just hidden
  - [ ] Genre pie-chart data (`/discover`) populates only for internet-transport bars — expected,
        not a bug (see dependency note above); a wifi/hotspot-only bar can still appear on the map
        (name/location/playlist) just without genre coloring
- [ ] Playlist registration (`/api/map/register`)
  - [ ] Same playlist name re-registered → refreshes in place (artists/note/timestamp updated)
  - [ ] New playlist name → appends as a new entry
  - [ ] A 4th distinct playlist → oldest-by-`updated_at` is evicted, only 3 kept
  - [ ] Legacy artist format (`["Artist Name", ...]`) and new format
        (`[{"name":..., "song_count":...}]`) both accepted correctly
- [ ] `is_live` on `/api/map` reads true only for internet-mode bars that have synced within the last
      5 minutes; goes false/stale after that without a manual unregister
- [ ] Genre profiling
  - [ ] `profiling_on` flag correctly reflects whether genre data is available for a bar
  - [ ] Genre pie/donut chart on `/discover` renders proportionally correct slices per bar
  - [ ] Profile cache respects `PROFILE_CACHE_TTL` — a fresh registration's genre data shows up within
        one cache refresh cycle, not instantly and not indefinitely stale
- [ ] Lat/lng location data persists across a re-registration that omits it (falls back to existing
      stored value rather than nulling it out)
- [ ] Map entries persist across a Render restart (written to disk immediately on register/unregister,
      not just kept in memory)

## 14. Resilience / Edge Cases

- [ ] Relay restart mid-session: in-memory `bar.requests` is wiped (expected/accepted — not
      auto-recoverable); host re-registers and resumes broadcasting its own state normally
- [ ] App backgrounded then foregrounded: `onForeground()`-style reconnect kicks in, playback resumes
      without manual intervention for ordinary (non-outage) blips
- [ ] Multiple customer devices submitting concurrently — no request lost, no duplicate processing
- [ ] Wireless debugging pairing expires mid-session — does **not** affect the running app/session,
      purely a dev-tooling concern

## 15. Admin/Bartender Authentication Robustness (gaps found by code audit 2026-08-01)

None of these are confirmed-working features to check off — they're **known gaps found by reading the
actual auth code on both platforms**. Each item below is "confirm this weakness still exists as
described, then decide whether it needs fixing" — not "confirm this is secure."

- [x] ~~Kiosk-native admin PIN has no lockout at all~~ — **fixed 2026-08-01**: progressive backoff on
      both platforms (first 3 wrong tries free, then 20s/40s/80s/160s... escalating delay before the
      field re-enables). See §A-adjacent scenario below for the "Forgot PIN?" flow this pairs with.
- [ ] **Contrast/confirm**: LAN admin auth on *both* platforms genuinely does lock out after 5 wrong
      attempts for 300s (Android `LocalRequestManager.checkAdminPin`, iOS `LocalServer.swift`'s
      equivalent) — confirm this actually triggers and actually clears after the cooldown.
- [x] ~~LAN bartender pairing has NO brute-force protection~~ — **fixed 2026-08-01, all three
      surfaces**: LAN `/api/bartender/pair` on both platforms, and the relay's
      `/api/bar/{id}/authenticate` (render bartender) — all independently apply a 3-attempt/15-min
      lockout keyed by **(bar, source IP)**, not global — one IP fumbling the PIN doesn't lock out a
      different bartender pairing from their own device or IP. See §Bartender pairing lockout below.
      Render's lockout key gained a third component, **role** (`(bar, ip, role)`), as part of the
      PIN-split work below — repeated bad bartender guesses from a shared bar IP no longer also lock
      out that IP's admin PIN attempts, and vice versa.
- [ ] **Android-specific**: force `barDetails.pin` to resolve to an empty string at runtime and confirm
      whether kiosk admin PIN entry then accepts **any** input as valid (`adminPin.isEmpty() || entered
      == adminPin` in `KioskView.kt`) — if this state is ever reachable in normal operation (not just a
      theoretical default), that's a real bypass, not just a defensive guard clause with no live path.
- [ ] **Bartender pairing never expires.** Once paired, a bartender device stays authorized for the rest
      of the session (potentially many hours) with no re-auth prompt — unlike the customer-facing
      session token, which does rotate mid-session. Confirm this is an accepted intentional tradeoff
      (convenience over rotation for a device that's presumably trusted staff hardware), not an oversight.

### Kiosk "Forgot PIN?" reset flow (new 2026-08-01, hardened same day)

Gated by **two** layers, not one: (1) the device's own unlock credential — Face ID/Touch ID/device
passcode (Android `BiometricPrompt` with `BIOMETRIC_STRONG or DEVICE_CREDENTIAL`; iOS
`LAContext.evaluatePolicy(.deviceOwnerAuthentication, ...)`) — must succeed *before* the reset form
even appears; (2) while the form is up, playback pauses and a repeating audible tone plays (Android:
`ToneGenerator`; iOS: generated tone, no bundled asset) so anyone nearby knows a sensitive action is
underway. The device-auth gate was added specifically because the beep alone isn't real friction — a
fast operator could complete the whole reset in a couple seconds otherwise. Confirming logs straight
into the admin panel with the new PIN active — no need to re-type it immediately.

- [ ] Tap "Forgot PIN?" — device unlock prompt (Face ID/Touch ID/passcode) appears **first**, before
      any PIN-reset UI is shown
- [ ] Cancel or fail the device-unlock prompt — back to normal PIN entry, nothing else happens, no
      beep, no pause
- [ ] Succeed the device-unlock prompt — **now** the reset form appears: music pauses, a repeating
      tone starts audibly within ~1s
- [ ] New PIN + confirm PIN fields — "Set PIN" stays disabled until both are 4-6 digits and match
- [ ] Confirm — PIN is persisted (`barDetails.pin` / `LocalStorage`'s `pinHash`), tone stops, music
      resumes if it was playing before, admin panel opens directly (no re-entry required)
- [ ] The *new* PIN is what's required on the next normal kiosk PIN entry — old PIN no longer works
- [ ] Cancel from the reset form (after device auth succeeded) — tone stops, music resumes, back to
      normal PIN entry, PIN unchanged
- [ ] This flow is kiosk-only — confirm no equivalent exists or is reachable on LAN/render admin
- [ ] Settings/Up Next/session are completely untouched by a PIN reset — this never goes through
      End Session or the setup wizard at all
- [ ] Android-specific: confirm `MainActivity`'s change from `ComponentActivity` to `FragmentActivity`
      (needed for `BiometricPrompt`) didn't disturb anything else app-wide — full regression pass on
      a build with this change, not just the PIN-reset flow in isolation

### Token-based bartender/admin authorization (new 2026-08-01) — critical, test thoroughly

**Context**: until this fix, entering the correct PIN was purely a client-side UI gate — the actual
action endpoints (approve/deny/settings/control/requests/history) never verified anything beyond
the same session token the public customer QR code also carries. Anyone who'd seen a bar's customer
link could call these directly, no PIN needed at all. This is the most severe gap found this
session; test it like a real security fix, not a UI nicety.

- [ ] **The core regression test**: with a valid `s`/session token but **no** auth token (or a
      garbage one), directly call each of these and confirm a 401/`Unauthorized` — not a 200:
  - [ ] Render: `POST /api/bar/{id}/approve`, `/deny`, `/control`, `/settings`; `GET /requests`,
        `/history`
  - [ ] Android LAN: `POST /api/request/approve`, `/deny`, `/api/admin/settings`; `GET /api/requests`
  - [ ] iOS LAN: same three POST + GET as Android
- [ ] Entering the correct PIN still gets you a real, working token — confirm every one of the
      above **succeeds** once the token from a successful `/authenticate` (render) or
      `/api/admin/auth`/`/api/bartender/pair` (LAN) is included
- [ ] **The "first bartender becomes admin" nuance** — both platforms' LAN: pair as the *first*
      bartender (no admin PIN ever entered), confirm you can still toggle Stripe/Bartender-pay/
      Accepting-requests from bartender.html using only your `bartenderId` — this must keep working,
      it's a legitimate flow, not a bug to close
  - [ ] A *second* (non-first, non-admin) paired bartender should be **rejected** (401) attempting
        the same settings toggle — only the first/admin bartender or a real admin token should work
- [ ] Token survives a page refresh (sessionStorage on render; re-derived from cached PIN on LAN) —
      confirm you're not silently kicked back to the PIN screen or losing action capability on reload
- [ ] Render admin.html: confirm the 429 lockout message now displays correctly (it was previously
      missing here — bartender.html had it, admin.html didn't, both share the same backend endpoint)
- [ ] **Known, deliberate gap — not yet fixed**: `/api/player/*` (play/pause/next/prev) and
      `/api/reports/generate` on LAN (both platforms) still accept any request with no token check.
      Lower priority than money/settings, but worth closing in a follow-up pass.

### Bartender pairing lockout (new 2026-08-01)

Per-**(bar, source IP)**, not global, on all three surfaces: LAN `/api/bartender/pair` (both
platforms) and the relay's `/api/bar/{id}/authenticate` (render). 3 wrong attempts → locked 15
minutes on that specific IP only.

- [ ] Fail bartender PIN 3 times from one device/IP — 4th attempt (even with the correct PIN) is
      rejected with a "too many attempts, try again in Ns" message, not a plain "incorrect PIN"
- [ ] While that IP is locked out, a **different** device/IP can still pair successfully with the
      correct PIN — confirm the lockout is genuinely per-IP, not global
- [ ] While that IP is locked out, **other already-paired bartenders' sessions are unaffected** — they
      can keep approving/denying requests normally
- [ ] After the 15 minutes elapse, the same IP can attempt again (no permanent lockout)

### Admin/bartender PIN split (new 2026-08-01) — all three surfaces

**Context**: admin and bartender used to share one PIN everywhere. Rotating "the" PIN from a new
Bartender-Sessions-tab-style control would have silently also changed the admin's own PIN. Fixed by
giving the bartender role its own independent secret, optional and **empty by default** — when
empty, the bartender role doesn't exist for that bar at all (no QR, no page, no pairing), not just
"weakly protected."

- [ ] **Fresh bar / fresh install, all three surfaces**: bartender PIN starts unset. No bartender QR
      code is shown anywhere (render admin.html's Actions tab, iOS `AdminView.swift`, Android
      `AdminScreen.kt`). Confirm the setup wizard (either platform) never prompts for a bartender PIN
      — it's deliberately not part of onboarding.
- [ ] With bartender PIN unset: `GET /bartender/{id}` (render) returns a real 404, not the bartender
      page shell. LAN `/bartender` (both platforms) also 404s. `POST /api/bartender/pair` (LAN, both
      platforms) and the relay's `/api/bar/{id}/authenticate` with `role: "bartender"` both reject
      (503/404) without attempting a PIN compare at all — confirm via direct call, not just UI.
- [ ] Admin sets a bartender PIN from each of the 5 admin surfaces in turn (kiosk-native ×2, LAN
      admin.html ×2, render admin.html): status flips to "on," the bartender QR/URL appears, and
      `/bartender/{id}` (or LAN `/bartender`) becomes reachable and accepts that PIN.
- [ ] **Propagation round-trip, render specifically**: set the bartender PIN from render admin.html
      while the host is running — confirm it lands in the relay's `desired_settings` immediately
      (control shows "pending"/greyed), then clears once the host's next sync echoes it back, and the
      bartender QR becomes live on the *host's own* Admin screen too (not just render's).
- [ ] **Propagation round-trip, kiosk/LAN specifically**: set the bartender PIN from the kiosk Admin
      screen (or LAN admin.html) — confirm it's usable for LAN bartender pairing immediately (no
      relay round-trip needed for LAN), and separately confirm it reaches the relay's
      `bartender_pin_hash` on the next register/sync so render admin.html's status also updates.
- [ ] **Clear/turn off**: with bartender access on and at least one bartender currently paired, turn
      it off from admin. Confirm: (a) the confirmation prompt fires before it takes effect, (b) the
      bartender QR/URL disappears from every admin surface, (c) `/bartender`/`/api/bartender/pair`
      start rejecting again, (d) **already-paired bartender sessions are NOT killed** — this only
      blocks *new* pairing, it doesn't revoke `bartender_tokens`/`LocalBartender` records that already
      exist (confirm this is the accepted behavior, not a bug — killing active sessions was never
      part of this PIN-split work, only "Kill session" on the still-unbuilt Bartender Sessions tab).
- [ ] **Independence from "Pay to bartender"**: toggle the `bartender_enabled` payment-mode setting
      on/off with the bartender PIN unset the whole time — confirm this toggle still works exactly as
      before (it governs whether customers see a "pay at bar" option, unrelated to whether a
      bartender-role login exists) and doesn't itself gate or get gated by the PIN.
- [ ] **Comparison is hash-based everywhere for this field** — matching Android's admin-PIN
      comparison (`BarDetails.pinHash`, itself hashed as of 2026-08-08; see the new section near
      the end of this file), the bartender field is also SHA-256-hashed on all three surfaces.
      Render admin.html hashes client-side
      (Web Crypto, https) before sending; both platforms' plain-http LAN admin.html pages send the
      raw PIN over `/api/admin/settings` and the **host hashes it server-side** instead — confirm a
      network capture of the LAN admin.html save request shows the raw PIN in flight (expected, LAN
      already requires physical network presence) but never leaves the LAN as anything but a hash on
      the wire to relay.
- [ ] Admin PIN's own wizard-only-change behavior and its progressive-backoff lockout are completely
      unaffected by any of the above — regression-check a normal admin PIN entry still works
      identically to before this change.
- [ ] A successful pairing before hitting 3 failures clears that IP's attempt counter (doesn't carry
      over to some future unrelated lockout)
- [ ] Test independently on all three surfaces — LAN Android, LAN iOS, and render — since these are
      three separate implementations (Android's `LocalRequestManager`, iOS's `LocalServer.swift`,
      the relay's `main.py`), not one shared code path
- [ ] Relay restart clears all render-side lockouts (in-memory only, same accepted tradeoff as
      `bar.requests`) — not a bug, matches existing relay-restart behavior elsewhere

### Bartender Sessions admin tab (new 2026-08-02) — LAN admin.html (both platforms) + render admin.html only, NOT kiosk-native

Surfaces existing paired-bartender and PIN-lockout bookkeeping behind a new "Sessions" tab, with
Kill (per bartender) and Let Retry Now (per waiting IP) actions, admin-token-only. **Framing note
(corrected 2026-08-02 after user feedback)**: "Waiting to Retry" is deliberately not a
"malicious IPs" security list — it's a courtesy tool for un-sticking a legitimate bartender who
fumbled their own PIN, not a hacker watchlist. The underlying per-IP throttle still runs regardless
of whether anyone ever looks at this tab; the tab is purely a convenience to shortcut someone's
wait, not a defense mechanism itself.

- [ ] Pair 2+ bartenders (different names/devices if possible), open the Sessions tab on each of
      the 3 admin surfaces — each shows every currently-paired bartender for **that surface's own**
      transport (LAN sessions and render sessions are separate pools — a bartender paired via LAN
      never appears in render's list or vice versa, confirmed by design, not a bug)
- [ ] Fail a bartender PIN 1-2 times (not enough to lock out) from one IP — that IP appears in
      Waiting to Retry with the correct attempt count and "not waiting yet" status, plus the name
      that was typed on the last failed attempt
- [ ] Fail 3 times to trigger the wait — same row now shows "waiting" with a counting-down
      remaining time
- [ ] **Kill a session**: confirm dialog fires, declining the "also change PIN" follow-up prompt
      still kills the session — that bartender's next poll gets logged out (session/pair screen
      reappears with an explanatory message, not a frozen stale UI) within one poll interval
- [ ] **Kill + bundled PIN change**: accept the "also change PIN" prompt, enter a new PIN — confirm
      BOTH happen: the killed bartender is logged out AND the old PIN no longer works for a fresh
      pairing attempt (new PIN required)
- [ ] **Let Retry Now**: confirm the target IP can immediately retry the PIN (not waiting out the
      remaining time), and that this does NOT change the PIN itself — the same PIN that was
      failing before still works once entered correctly
- [ ] **Admin-only enforcement, all three surfaces**: pair as the *first* bartender (LAN only — gets
      auto-promoted to `isAdmin`/settings-capable) and confirm that bartender's own token/id gets
      401 on `GET .../bartender_sessions` (or render's equivalent) — the isAdmin-promoted-bartender
      shortcut that already exists for the 3 payment toggles must NOT extend to viewing/killing
      sessions or clearing lockouts. This is the single most important regression to verify — it's
      an easy shortcut to accidentally take (reusing `isValidSettingsToken` instead of
      `isValidAdminToken`) since the two checks look similar everywhere else in this codebase.
- [ ] Kiosk-native admin (iOS `AdminView.swift`, Android `AdminScreen.kt`) has **no** Sessions tab —
      confirm this is still true (deliberately out of scope, not an oversight)
- [ ] **Closed 2026-08-09 — LAN now matches render's opaque-id split, see the new section near the**
      **end of this file for full test coverage of the fix.** `bartenderId`/`BartenderRecord.id`
      is still the real bearer token used for approve/deny/list, unchanged — but LAN's
      `GET .../bartender_sessions` and `POST .../kill` now go through a separate `session_id`, same
      as render already did, so a bartender's actual working credential is no longer present in the
      Sessions tab's network traffic at all
- [ ] Relay restart / LAN session reset (End Session) clears all sessions and lockout state for
      that surface — same in-memory-only tradeoff as everywhere else in this system

### Kiosk lock-to-app (new 2026-08-02, item 14) — iOS + Android, deliberately platform-asymmetric

Not unbreakable on either platform, and not meant to be — this raises the bar against a casual
patron poking at the kiosk, not a determined attacker. True forced lockdown on either platform
requires MDM/device-provisioning, explicitly out of scope this pass (see CLAUDE.md).

- [ ] `remoteOnly` mode: neither platform's lockdown mechanism engages at all — confirm no pinning
      attempt (Android) and no Guided Access warning (iOS) in this mode
- [ ] **Android, `localOnly` or `localAndRemote`**: launching the kiosk (tapping "Launch Kiosk" at
      the end of setup) triggers `startLockTask()` — first time ever on a fresh device (Screen
      Pinning not pre-enabled in Settings), a one-time OS confirmation dialog appears; if the
      operator pre-enabled Settings → Security → Screen pinning beforehand, it pins silently with
      no dialog
  - [ ] While pinned, confirm the standard Android exit gesture (back+overview held, or
        swipe-up-and-hold on gesture nav — varies by device/OS version) is required to leave the
        app; a plain back-button tap or app-switcher swipe does NOT escape
  - [ ] End Session (both the kiosk-native button and, per the earlier "remote Stop/End Session
        removed" decision, there is no remote trigger to test) → `stopLockTask()` fires, device
        unpins cleanly, no crash, no dialog
  - [ ] Cold app launch always shows the setup wizard first (never resumes directly into a pinned
        kiosk) — confirm pinning never fires before the operator explicitly launches the kiosk
- [ ] **iOS, `localOnly` or `localAndRemote`, Guided Access OFF**: kiosk shows a small warning
      caption (same visual slot/style as the existing server-error caption) recommending Guided
      Access — confirm it's visible but not blocking (Request button and other kiosk UI still fully
      usable underneath it)
  - [ ] Enable Guided Access from Settings, then triple-click the side button while the kiosk is
        foregrounded — confirm the warning caption disappears **live**, without needing to relaunch
        the app or leave/re-enter the kiosk view (tests the `guidedAccessStatusDidChangeNotification`
        observer, not just the one-time `onAppear` check)
  - [ ] Exit Guided Access (triple-click again, authenticate if a passcode was set for it) — confirm
        the warning caption reappears
  - [ ] **Cannot be tested in iOS Simulator** — Guided Access is real-hardware-only; this entire
        section needs a physical device
- [ ] **Setup wizard advisory, both platforms, Local Only / Local + Remote steps only** (not Remote
      Only): a recommendation to enable the platform's lockdown mechanism is visible on both cards
  - [ ] iOS: this is a **live conditional check** — with Guided Access already enabled before
        reaching this wizard step, confirm the warning text is absent; with it off, confirm the
        warning text is present. (Reads `isGuidedAccessEnabled` fresh per render — if the operator
        backgrounds the app, enables Guided Access, and returns mid-wizard, confirm behavior is at
        least reasonable, even if not instantly live like the runtime KioskView warning.)
  - [ ] Android: this is a **static recommendation, not conditional** — confirm the caption always
        appears on these two cards regardless of whether Screen Pinning happens to already be
        enabled in Settings (there's no API to check, so don't expect or require it to hide itself —
        that's expected platform-limited behavior, not a bug)
  - [ ] Remote Only's card has no such advisory on either platform

### Android: empty admin PIN bypass (new 2026-08-02, item 3) — was a real reachable bug, now fixed

- [ ] **Regression, the core bug**: on an existing bar (has a saved PIN), re-run the setup wizard
      (End Session → wizard), reach the Admin PIN step, **clear the pre-filled PIN field
      completely** and leave both fields blank, tap Next — confirm the OLD PIN is still what's
      required at the kiosk admin lock screen afterward, not a blank/bypassable one
- [ ] Same scenario, but confirm the wizard's own footer text ("Leave blank to keep your current
      PIN, or enter a new one to change it") is now actually true — previously the field left blank
      silently wiped the stored PIN to empty instead of doing what the text promised
- [ ] **Defense-in-depth check**: even if some other future bug ever left the stored PIN empty
      again, confirm typing an arbitrary PIN at the kiosk admin lock screen is now **rejected**, not
      accepted — the `adminPin.isEmpty()` bypass is gone; only the exact stored PIN unlocks admin,
      or the "Forgot PIN?" biometric-gated reset flow (still fully functional, doesn't depend on
      knowing/matching the old PIN at all)
- [ ] Normal cases still work: entering a genuinely new PIN (not leaving it blank) on the wizard
      step still changes the PIN as expected; first-time setup (no saved PIN yet) still requires a
      valid 4-6 digit PIN before the wizard lets you proceed past this step
- [ ] iOS: confirmed via code read (not a live bug there) that `advanceFromPin()`/`verify()` never
      had either half of this bug — no regression test needed on iOS specifically, but worth a
      quick sanity pass that "leave blank to keep PIN" still behaves correctly there too, unrelated
      to this fix

### Item 4: bartender pairing expiration — confirmed intentional, no test needed

Investigated and closed with no code change — a paired bartender's credential correctly stays
valid until End Session or an explicit admin Kill (Bartender Sessions tab), with no automatic
timeout. This is the desired behavior (a bartender should stay paired for their whole shift), not
a gap. Nothing to regression-test here specifically — covered by the existing Bartender Sessions
tab tests above.

### 30-minute idle-pause auto-mechanism REMOVED entirely (new 2026-08-02) — replaced by pause-blocks-requests

The old timer (armed whenever playback stopped, fired after 30 continuous minutes unresumed —
iOS just re-paused, Android also rotated the session/QR, the two platforms were inconsistent with
each other) is gone on both platforms, not just changed. Do not write regression tests expecting
any session rotation, queue wipe, or re-pause action 30 minutes after a pause — none of that
exists anymore, by design.

- [ ] **Core behavior, both platforms**: pause playback (any method — admin/bartender manual pause
      tap is the primary path, but this should hold regardless of cause) — confirm the kiosk's own
      local Request button immediately hides/disables (not just after some delay)
- [ ] **Remote/QR customers, render**: while paused, load `customer.html` (or poll it if already
      loaded) — confirm the Request/Pay button similarly hides/disables, not just fails with an
      error after tapping. Confirm catalog browsing itself is completely unaffected — songs still
      list, search/filter still works, only the ability to submit a new request is blocked
- [ ] **Resume restores it immediately**: un-pause — confirm both the local kiosk button and
      remote customer.html's Request button become available again within one poll cycle, with no
      manual re-toggle needed from the admin
- [ ] **Admin's actual `accepting_requests` setting is never touched by this** — with the admin
      toggle explicitly ON, pause and confirm: (a) requests are blocked as above, but (b) the
      Actions-tab toggle itself still visually shows ON, not flipped to OFF — this is the
      raw-vs-effective split (mirrors `effectiveStripeEnabled`); pausing must never silently
      change what the toggle displays or what gets restored on resume
  - [ ] With the admin toggle explicitly OFF (requests already disabled for an unrelated reason),
        pause and resume — confirm it's still OFF afterward, not accidentally turned back on by
        the pause/resume cycle
- [ ] **Stripe payments already in flight are unaffected**: start a Stripe payment (create a
      payment intent) while playing, then pause before confirming payment — confirm
      `bar_payment_confirmed()` still succeeds; this endpoint is deliberately NOT gated by the new
      effective-accepting-requests check, unlike request/create-payment-intent
- [ ] **No wipe of existing Up Next**: with paid/approved requests already in the queue, pause for
      an extended period (longer than the old 30-min window used to matter) — confirm Up Next is
      completely untouched; this new mechanism only ever blocks *new* submissions, never removes
      existing ones
- [ ] **Leave the app paused indefinitely (well past 30 minutes)** — confirm nothing automatically
      happens: no session/QR rotation, no forced re-pause action (redundant since it's already
      paused), no app termination, no blocking screen. The kiosk should behave identically at 5
      minutes paused and 5 hours paused — the only way out is admin resuming or ending the session
- [ ] Regression: End Session still works exactly as before and is unaffected by any of this —
      it's the one and only mechanism left for "wipe everything and start over"

- [ ] **LAN transport (WiFi/Hotspot) — added 2026-08-06 after an initial gap was found and
      closed.** The first pass only covered the relay (internet transport) and the local kiosk
      button — LAN-mode remote customers, served entirely by the host's own `LocalServer` and
      never touching the relay at all, were still gating purely on the raw
      `acceptingRequests`/`barDetails.acceptingRequests` value with no play-state check. Fixed the
      same day: iOS `LocalServer.swift`'s `/api/nowplaying` echo, `/api/request`, and
      `/api/create-payment-intent` now all use `(cfg?.acceptingRequests ?? true) &&
      MusicService.shared.isPlaying`; Android `LocalServer.kt`'s `handleNowPlaying()`,
      `handleSubmitRequest()`, and `handleCreatePaymentIntent()` now use `(...) &&
      coordinator.isPlaying`. Test independently on LAN, since it's a separate code path from
      internet transport, not just "the same fix applies automatically":
  - [ ] On WiFi or Hotspot transport specifically (not internet), pause — confirm LAN
        `customer.html` (both platforms) hides/disables its Request button on the next
        `/api/nowplaying` poll, same as the internet/relay path already does
  - [ ] Confirm LAN's cached full-catalog response (`preloadCatalog()`/`catalogJson()`) is
        deliberately **left untouched** (still raw, not effective) — this is correct, not a missed
        spot: the cache would go stale the instant play state changed if it baked in a live value,
        so the live `/api/nowplaying` poll is where the effective gating actually has to live for
        LAN, same division of labor as the relay's cached-vs-live split
  - [ ] Directly call LAN's `/api/request` and `/api/create-payment-intent` while paused (not just
        through the UI) — confirm both reject (403/400 depending on platform) even if a stale
        client tries to submit anyway
  - [ ] LAN's `/api/payment-confirmed` remains **ungated** on both platforms, matching the relay's
        `bar_payment_confirmed()` — an already-succeeded Stripe payment must still be honored

### Android: empty-queue setup now blocked (new 2026-08-06, item 8) — Android only, iOS not checked

- [ ] Skip both the Spotify device step and the local-folder step in setup, reach the Summary
      step, tap Done — confirm a blocking dialog appears ("No music to play") instead of silently
      completing setup; confirm the kiosk is NOT launched (still on the Summary/wizard screen)
- [ ] From that dialog, go back and actually select a Spotify playlist or local folder with at
      least one track — confirm Done now proceeds normally
- [ ] Select a Spotify playlist that has zero tracks in it (rather than skipping the step
      entirely) — confirm the same blocking dialog fires; this is checking the actual resulting
      catalog size, not just "did you tap Skip"
- [ ] **Regression, most important one**: while the kiosk is already live, tap into the admin
      overlay (long-press or however it's normally reached) and tap Done/close — confirm this
      still just closes the overlay as before, with **no** empty-catalog check applied here. The
      guard added for setup completion must never affect closing the live admin panel for an
      unrelated reason (they share the same `AdminScreen` composable but the check only wraps the
      setup-completion call site)
- [ ] If a catalog somehow does end up empty while the kiosk is already running (e.g. a playlist
      gets emptied out from Spotify's side after setup) — confirm the now-playing tile shows "No
      music to play" instead of the previous ambiguous "—" placeholder, in both portrait and
      landscape kiosk layouts
- [ ] Confirm the ordinary "—" placeholder still shows correctly for a normal brief no-current-song
      moment (e.g. between songs) when the catalog is NOT empty — only the genuinely-empty-catalog
      case should show the new message
- [x] **iOS: confirmed not needed, no code change (2026-08-08).** iOS doesn't have Android's
      two-source setup shape (a separate Spotify-device step and a separate local-folder step, either
      of which can be independently skipped) — so there's no equivalent "skipped both, silently ended
      up with an empty catalog" path to guard against. User's call: if an operator picks an empty
      Apple Music playlist on iOS, that's a straightforwardly avoidable operator choice, not a
      structural gap worth a blocking dialog for. No parity gap with Android.

### Report generation redesign + relay mirror (new 2026-08-07, items 9/10/11) — both platforms + relay

**Why the relay mirror exists, corrected 2026-08-07**: not a data-durability/backup concern
(reports living only on the kiosk is already the normal, accepted case for WiFi/Hotspot/Local
transport, same as everywhere else in this system) — it's specifically so an admin using **internet**
transport can pull a report down to their own phone/device **without needing physical or LAN access
to the kiosk at all**. The primary scenario below tests exactly that.

- [ ] **Primary use case — genuinely remote download**: with the bar on internet transport, from a
      device that is NOT on the same network as the kiosk (a different WiFi, cellular data, etc.) —
      open render admin.html, generate and/or download a report. Confirm this works end-to-end with
      zero physical/LAN proximity to the kiosk required at any point — this is the actual reason the
      feature exists, not just "list some files"
- [ ] **No-op, empty session**: fresh session, no requests at all — generate a report (any
      trigger) — confirm no file is written and "No report created — no unreported requests yet"
      shows on the button that triggered it
- [ ] **No-op, pending-only**: submit a request, approve it, don't let it play yet — generate a
      report — confirm this is ALSO a no-op (no file, not even a snapshot-only file) — the gate is
      specifically "any played+denied requests," not "any requests at all"
- [ ] **Core split**: get a mix — some requests played, some denied, some still pending/approved —
      generate a report. Confirm the resulting CSV contains all of them (played+denied AND the
      pending/approved snapshot), but only played+denied are gone from the LIVE admin view
      afterward (LAN admin.html's own Played/Denied history sections, and the kiosk's own report
      list source) — pending/approved still show normally, unaffected
- [ ] **Repeatability**: generate a report, then immediately generate again with nothing new having
      played/denied in between — confirm the second call is a no-op (the first call already wiped
      what would have been reported) — no duplicate/overlapping file
- [ ] **Then generate a third time after something new plays** — confirm the new report contains
      ONLY the newly-played/denied request(s), not a re-dump of what was already reported and wiped
- [ ] **Retention**: generate 21+ reports in one long test session (or manually drop file count to
      confirm behavior at the boundary) — confirm only the newest 20 remain, oldest deleted, even
      if never downloaded
- [ ] **Remote trigger, render admin.html**: tap "Generate Report Now" on the new Kiosk Reports
      section — confirm it takes effect on the kiosk (up to ~5s) and becomes visible/downloadable
      on render (up to another ~5s, so ~10s worst case) — same latency shape as approve/deny/control,
      nothing special-cased
- [ ] **Render list/download/cleanup**: with reports waiting, confirm render's Kiosk Reports
      section lists them (name, relative time, size) — download one — confirm it disappears from
      render's list AND from the kiosk's own local report list/storage (both sides cleaned up by
      one action, no separate delete button on render)
- [ ] **Kiosk-side delete still propagates**: with a report showing on both kiosk and render, delete
      it directly from the kiosk's own admin screen (not via render) — confirm it disappears from
      render's list on the next sync too (via `report_filenames` reconciliation, not a
      `delete_report` action — this direction is a passive full-reassert, not an explicit command)
- [ ] **Relay restart recovery**: with reports on the kiosk and mirrored to render, restart the
      relay process (or simulate by clearing render's session) — confirm the next kiosk sync
      re-populates render's list from scratch, including reports that existed *before* the restart,
      not just newly-generated ones (`reports_needed` backfill)
- [ ] **New session backfill**: end the kiosk session and start a genuinely new one (new session
      token) while old undownloaded reports still exist locally — confirm those old reports still
      show up on render once the new session registers (same backfill mechanism as the restart
      case, since a new session also replaces the relay's `BarSession` object outright)
- [ ] Confirm render's pre-existing "↓ Download CSV" button (the live history export, in the
      Reports tab above the new Kiosk Reports section) is **completely unaffected** by any of the
      above — it's a different, always-live mechanism, not reading from the file mirror at all
- [ ] Confirm the new report endpoints reject a non-admin bartender token (403) — these are
      admin-only, stricter than the three payment toggles a bartender token can already touch

### Spotify never silently skips a song (new 2026-08-08, item 12) — Android only, unified 2-strike design

The design went through three rounds the same day before landing here — see CLAUDE.md for the
full progression. **The final rule: one consecutive-failure counter shared by both the startup**
**and mid-song failure paths.** 1st failure (any type, any path) → mark+skip if paid, keep
playing. 2nd consecutive failure → full stop (outage-recovery screen), regardless of payment
status. Any successful Spotify play resets the counter to 0.

- [ ] **Single isolated failure, either path (1st strike)**: force exactly one Spotify failure —
      either at song startup ("no device") or mid-song (unreachable/wrong-track/stuck-paused) —
      then let the *next* Spotify song play normally. Confirm: the outage-recovery screen never
      appears, `spotifyOutageActive` stays false, Play stays enabled everywhere, and the kiosk just
      moved on to the next song
- [ ] **Two consecutive failures (2nd strike) — mix the failure types**: force a failure, let it
      skip to the next song, then force *another* failure on that very next song (can be a
      different failure type, and can span across the startup/mid-song boundary — e.g. first
      failure is mid-song, second is a startup failure on the following track, or vice versa).
      Confirm the **second** one trips the full outage-recovery screen instead of skipping again —
      this is the core of the unified design, don't test the two paths in isolation only
- [ ] **Counter resets on success**: one failure, then a Spotify song plays successfully, then
      another isolated failure — confirm the second failure does NOT trip the outage screen (i.e.
      it's correctly treated as a fresh 1st strike, not a continuation of the earlier one)
- [ ] **Reconnect does not pre-arm the counter**: trigger the outage screen (2 consecutive
      failures), reconnect via the existing PIN-gated flow, then immediately force one more
      failure — confirm this single post-reconnect failure trips the outage screen again right
      away (1 strike, not 2) rather than requiring two more — deliberate: if reconnecting didn't
      actually fix anything, don't make staff sit through a second false start before finding out
- [ ] **Filler song, any failure type**: with a non-requested (shuffle filler) Spotify song
      currently playing/starting, force a failure — confirm no request gets marked, no song gets
      pulled from Up Next — but this failure still counts toward the 2-strike escalation counter
      (confirm via the "two consecutive failures" test above using filler songs specifically, since
      the counter must count these too, not just paid-request failures)
- [ ] **Free request, any failure type**: same as filler — no marking, no Up Next removal, but
      still counts toward the escalation counter
- [ ] **Paid request, 1st-strike failure**: approve a paid request for a Spotify song, let it
      become current, force a failure (either path). Confirm: (a) it's marked with an "⚠ Couldn't
      play" badge (not Played, not Denied, not stuck "In queue") on LAN admin.html and render
      admin.html (if on internet transport) — with requester name and price visible — (b) the
      kiosk moves on to the next song rather than stopping, since this was only the 1st strike
- [ ] **Multi-song paid request, middle song fails**: a 2-3 song paid request where the *first*
      song already played successfully and the *second* hits a failure — confirm: (a) the whole
      request gets marked unfulfilled (not silently ignored for not being the last song — this is
      `markUnfulfilled()`'s any-song matching, unlike `markPlayed()`'s last-song-only matching),
      (b) the *third*, not-yet-played song is pulled off Up Next too (`cancelRequestedSongs()`),
      not left sitting there to be attempted later
- [ ] **Report generation picks it up**: with an unfulfilled request sitting on the kiosk, generate
      a report (any trigger) — confirm it's included in the CSV and gets wiped from live memory
      afterward, same as played/denied — and that a *second* immediate report generation is a
      no-op if nothing else changed (consistent with the existing report no-op gate)
- [ ] Confirm the render admin.html Reports tab's summary cards show a count for "Couldn't Play"
      alongside Approved/Denied/Played/Pending, and that the item appears in the Past Requests list
      with the same badge
- [ ] Confirm Play is disabled on LAN, render, and the kiosk's own admin screen while the outage
      screen is active (2nd-strike trip only — 1st-strike skips should never disable Play anywhere),
      and that reconnecting via the existing PIN-gated flow clears it and resumes correctly
- [ ] Kiosk-native `AdminScreen.kt` has **no** request-status display at all — confirm this is
      still true and expected, not a missed spot (LAN/render admin.html are the intended surfaces
      for this)

### LAN player/reports auth gate + Android admin-PIN hashing (new 2026-08-08) — both platforms, no relay involvement

Two previously-flagged "deliberately out of scope" gaps, closed together. See CLAUDE.md for the
full rationale (why player/reports had no gate, why Android's admin PIN was still plaintext, and
the on-device migration/UX-prefill tradeoffs).

**LAN player/reports auth:**
- [ ] From a browser NOT authenticated to admin.html (no valid `token` in hand), hit
      `/api/player/play` (or pause/next/prev) directly with a POST — confirm 401, and confirm the
      kiosk does **not** actually pause/skip
- [ ] Same for `GET /api/reports`, `POST /api/reports/generate`, `GET /api/reports/{filename}`,
      `DELETE /api/reports/{filename}` — all four reject with 401 when `token` is missing/invalid
- [ ] From a properly authenticated admin.html session (valid PIN entered), confirm all of the
      above — play/pause/next/prev buttons, the Reports tab's list/download/delete/generate — still
      work exactly as before; this should be purely invisible to a legitimate admin
- [ ] Confirm a paired **bartender** token (not admin) is rejected by all of these — bartender.html
      never had a player or reports UI to begin with, so this should never come up in normal use,
      but the backend should still reject it explicitly, not just "never get called with one"
- [ ] iOS specifically: confirm `/api/player/*`'s pre-existing session-expired check (a stale/
      wrong-network `s` param) still returns its own distinct message, separate from a missing/bad
      `token` returning a plain 401 — the two checks are independent, don't let a fix to one mask
      the other's distinct error message

**Android admin-PIN hashing + migration:**
- [ ] **Existing install migration**: on a device with a pre-2026-08-08 build already set up (a
      real plaintext PIN saved), install the updated build and relaunch — confirm the existing PIN
      still works for kiosk-native unlock, LAN admin.html auth, and the render admin.html PIN
      (relay `pin_hash`) without needing to re-enter or reset it. No Forgot-PIN flow should be
      required just from updating.
- [ ] **Fresh install**: set a new admin PIN via the wizard, confirm kiosk-native unlock, LAN
      admin.html auth, and render admin.html auth all accept it
- [ ] **Wizard re-run, leave PIN blank**: re-run setup on an existing bar, leave both PIN fields
      blank (now start empty, not prefilled — this is a deliberate UX change, confirm it's
      expected not a regression), tap Next — confirm the *previous* PIN still works afterward, not
      wiped
- [ ] **Wizard re-run, set a new PIN**: type a new PIN + confirm, tap Next — confirm the *new* PIN
      works and the *old* one no longer does, everywhere (kiosk-native, LAN, render)
- [ ] **Kiosk Forgot-PIN flow**: use the existing device-biometric-gated Forgot-PIN reset (from the
      admin-lockout screen) to set a new PIN — confirm it works and propagates the same way as a
      wizard-set PIN (kiosk-native, LAN, and — since this also calls `propagateBarDetails()` —
      render, once the host's next sync tick lands)
- [ ] Confirm the relay's copy of `pin_hash` still matches what LAN/kiosk actually accept after
      any of the above — this is the exact bug class that would recur if the register/sync payload
      ever re-hashed an already-hashed value (`RelayClient.kt` previously did `sha256(barDetails.
      pin)` on every call; now sends the already-hashed `barDetails.pinHash` directly)

### LAN bartender-credential exposure closed (new 2026-08-09) — both platforms, no relay involvement

Closes the previously "known, accepted" gap in the Bartender Sessions section above. LAN's Sessions
tab now uses an opaque `session_id` for its own list/kill endpoints, same as render already did —
`bartenderId`/`BartenderRecord.id` (the actual bearer token) is unchanged everywhere else.

- [ ] **The core fix**: with a bartender paired on LAN, open a network inspector (browser dev tools
      on the admin device, or a packet capture on the LAN) while loading the Sessions tab — confirm
      the `GET /api/bartender/sessions` response contains a `session_id` field, and that this value
      does **not** work as a bearer token — try it directly against `/api/requests?token=<that
      value>` or `/api/bartender/pair`'s approve/deny flow and confirm it's rejected (401), unlike
      the bartender's actual `bartenderId`
- [ ] **Kill still works end-to-end**: from the Sessions tab, Kill a paired bartender — confirm
      their *next* poll/approve/deny call gets 401 and they're bounced to the pair/PIN screen with
      the explanatory message (unchanged existing behavior), even though Kill's request body now
      carries `session_id` instead of the real id
- [ ] **Approve/deny/list still work unmodified**: confirm a paired bartender can still
      approve/deny requests and see the request list normally — these flows still authenticate with
      the real `bartenderId`, completely untouched by this fix; only the Sessions tab's own two
      endpoints changed
- [ ] **iOS migration — existing paired bartender from before this update**: if you have a
      pre-2026-08-09 build with an already-paired bartender, install the updated build without
      re-pairing, then open the Sessions tab — confirm the bartender still appears (not silently
      dropped), gets a fresh `session_id` assigned on that first read, and Kill still works against
      them afterward. Android needs no equivalent test — its bartender list is in-memory only and
      is already cleared by the same app-update process restart, so there's no persisted
      pre-migration state to reconcile
- [ ] **Multiple paired bartenders**: with 2+ bartenders paired, confirm each gets its own distinct
      `session_id` and Kill only removes the intended one, never a different row
- [ ] Confirm `bartender_id` is still the field name used by the actual pairing/approve/deny/status
      flows (`POST /api/bartender/pair`'s response, `GET /api/bartender/status`, etc.) — this fix
      deliberately only touches the two Sessions-tab endpoints, not the wire contract bartenders'
      own devices depend on
