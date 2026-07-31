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

## 1. Setup & Onboarding `[Both]`

- [ ] Fresh setup wizard, start to finish, all steps in order
  - [ ] Kiosk Display Mode step (Local Only / Local+Remote / Remote Only)
  - [ ] Network/Transport step — a transport is **always** chosen here regardless of display mode
  - [ ] Local content/folder step (Android) / equivalent library step (iOS)
  - [ ] Spotify (Android) / Apple Music (iOS) device pairing step
  - [ ] Playlist selection step
  - [ ] Bar name entry
  - [ ] Admin PIN entry (4–6 digits)
  - [ ] Approval mode step (Free / Bartender Pay / Stripe)
  - [ ] Pricing step (per-song, 3-song bundle, currency)
  - [ ] Summary/confirm step, session actually starts
- [ ] End Session → wizard reopens with **all previous values pre-filled**, not blank
- [ ] Stop Session (web admin "Stop Session" action) → Spotify/Apple Music auth token also cleared,
      re-login required next setup
- [ ] Re-pairing host device to IDE for deployment doesn't require a fresh setup (sanity, not a product test)

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
  - [ ] Currency-specific minimum enforced per `STRIPE_MINIMUMS` curated list (test at least 2 currencies)
  - [ ] Apple Pay domain file served correctly (`/.well-known/apple-developer-merchantid-domain-association`
        or equivalent) — full end-to-end Apple Pay activation is still blocked on Stripe dashboard domain
        registration per project notes; confirm current status before assuming this is fully live
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
- [ ] iOS: confirm whether an equivalent outage state can occur at all (no equivalent breaker built this
      session — determine if iOS's Spotify/Apple Music integration needs the same treatment)

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
