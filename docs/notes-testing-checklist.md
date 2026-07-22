# Testing checklist — features since 2026-06-26

Last reviewed 2026-07-22 — added §7 (kiosk display mode / Local Only) and §8 (Up Next queue-order +
Past Requests fixes), both shipped same-day as this review. Previous review was 2026-07-18 against
the settings-propagation redesign (§2) and the new accepting_requests Internet-mode toggle (§1b).

Surface key (13-surface matrix, see CLAUDE.md):
- **A-K-i / A-K-a** = Admin kiosk-native (iOS AdminView.swift / Android AdminScreen.kt)
-
- **A-L-i / A-L-a** = Admin LAN HTML (iOS WebApps/admin.html / Android assets/admin.html)
- **A-N** = Admin Internet/relay (static/admin.html)
- 
- **B-L-i / B-L-a** = Bartender LAN HTML
- **B-N** = Bartender Internet/relay
- 
- **C-K-i / C-K-a** = Customer kiosk-native (iOS KioskView.swift / Android KioskView.kt+LocalRequestSheet.kt)
- **C-L-i / C-L-a** = Customer LAN HTML
- **C-N** = Customer Internet/relay

Coverage matrix (which features touch each surface):

| Surface | 1. End Session/shutdown | 1b. accepting_requests | 2. Settings lock | 3. Payment-mode flags | 4. requester_name | 5. Up Next badge |
|---|---|---|---|---|---|---|
| A-K-i | X | | | X | | |
| A-K-a | X | | | X | | |
| A-L-i | | | X | X | | |
| A-L-a | | | X | X | | |
| A-N | | | X | X | | |
| B-L-i | | | X | X | X | |
| B-L-a | | | X | X | X | |
| B-N | | | X | X | X | |
| C-K-i | | X | | X | X | X |
| C-K-a | | X | | X | X | X |
| C-L-i | | X | | X | X | X |
| C-L-a | | X | | X | X | X |
| C-N | | X | | X | X | X |

Kiosk-native columns (C-K-i / C-K-a) for rows 1b/4/5 were flagged `?` originally, then verified against
source on 2026-07-17 — all three are already implemented on kiosk-native, not web-only. See
`CLAUDE.md`'s "Kiosk-native customer surface — confirmed cross-cutting invariants" section for the
exact call sites. Kept as `X` (functional test still needed) rather than assumed-done from the source
read alone — code correctness isn't the same as verified runtime behavior.

---

## 1. Shutdown / End Session — HIGHEST PRIORITY (real money involved via 1b)

**Surfaces:** Admin kiosk-native only (iOS `AdminView.swift`, Android `AdminScreen.kt`)

- [ ] iOS: tap End Session mid-service → confirm relay session unregisters (check it drops off `/discover` map / bar list)
- [ ] iOS: confirm app returns to setup wizard on restart, not a stale operating/network mode
- [ ] Android: same two checks
- [ ] iOS: End Session while a customer's Stripe PaymentSheet is open but not yet confirmed → confirm charge still completes (`/payment-confirmed` succeeds) and isn't silently dropped
- [ ] Android: same in-flight-payment check
- [ ] Confirm bartender/customer pages reflect "session ended" within one poll cycle on all connected browsers

## 1b. accepting_requests flag (pairs with shutdown, but independently testable)

**Surfaces:** Customer LAN (iOS + Android), Customer Internet/relay, Admin/Bartender Internet+LAN
(the toggle control itself — added 2026-07-18, wasn't in the original 2026-07-08 accepting_requests
build), kiosk-native

- [ ] **NEW:** relay `admin.html` Actions tab has an "Accepting requests" toggle (separate card, below
      the Payments card) — flip it and confirm it locks (dimmed) until the host confirms, same UX as
      the Stripe/Bartender toggles
- [ ] **NEW:** relay `bartender.html` Actions tab has the same toggle — confirm bartenders can flip it,
      not just admin
- [ ] **NEW:** iOS + Android LAN `admin.html`/`bartender.html` — same toggle present and working
- [ ] Toggle off accepting new requests → Browse/Pay/Submit buttons disappear, "Not accepting requests right now" shown within ~5s poll (relay customer.html)
- [ ] Same on iOS LAN customer.html
- [ ] Same on Android LAN customer.html
- [ ] QR code + now-playing stay visible throughout (not hidden along with the buttons) — **this
      regressed once already (see §2's now-playing regression test) — don't skip this check**
- [ ] `/request` and `/create-payment-intent` return 403 while off
- [ ] `/payment-confirmed` still succeeds for a request already in flight when the flag flips off
- [ ] Kiosk-native (iOS `KioskView.swift`'s `allowsLocalRequest`, Android `KioskView.kt`'s `showLocalRequestButton`) already gates the local Request button on this flag in source — verify live: toggle off, confirm the kiosk still shows now-playing/QR but hides/disables its own Request button too

## 2. Settings confirmation ("desired_settings", redesigned 2026-07-18 — no more version numbers)

**Surfaces:** Admin LAN (iOS + Android), Admin Internet, Bartender LAN (iOS + Android), Bartender Internet

Two things changed same-day and both need verifying, not just the lock/unlock UX (which should look
identical to before — the redesign was internal):

- [ ] **Regression test — this is the actual bug the user found live:** with a song already playing and
      a request already approved/queued, flip *any* of the three toggles (Stripe, Bartender, or
      Accepting requests) from the Internet `admin.html` or `bartender.html` → confirm **Now Playing and
      Up Next do NOT go blank** on any relay page (customer/admin/bartender) at any point during or
      after the toggle. Repeat for each of the three fields individually — the bug wasn't specific to
      one field, it would have hit all three before the fix.
- [ ] Same check toggling from the **kiosk-native** admin screen and from **LAN** admin.html directly
      (both platforms) — these paths also used to trigger a full re-register
- [ ] Flip a payment toggle on relay `admin.html` → toggle visibly locks (dimmed, unclickable) instantly
- [ ] A second browser tab on relay `admin.html` for the same bar also shows the toggle locked, not just the tab that clicked it
- [ ] relay `bartender.html` for the same bar also shows the lock
- [ ] Toggle unlocks within ~5s (one host sync cycle) once host picks it up and echoes back
- [ ] Repeat on iOS LAN admin.html / bartender.html
- [ ] Repeat on Android LAN admin.html / bartender.html
- [ ] Rapid double-toggle (change, then change again before the first confirms) → confirm the **final**
      state matches the **last** click, not the first — this is the specific race the new
      `desired_settings` design was built to handle correctly (compare against current desired value,
      not a stale snapshot)
- [ ] Kill host mid-sync (simulate dropped sync/airplane mode briefly) → confirm toggle self-heals on next 5s heartbeat rather than staying locked forever
- [ ] Optional/technical: watch server logs while rapidly toggling — should see repeated `POST /api/host/sync` calls but **no** `POST /api/host/register` calls during toggling (register should only fire at startup/End Session/catalog refresh)

## 3. Payment-mode refactor (raw stripeEnabled/bartenderEnabled flags)

**Surfaces:** effectively all 13 — prioritize the 4x4 combo grid below over exhaustive per-surface passes

Run these 4 combinations, each checked on: relay admin+bartender+customer, iOS LAN admin+bartender+customer, iOS kiosk request sheet, Android LAN admin+bartender+customer, Android kiosk request sheet:

- [ ] Stripe only (bartender off) — price shown, payment required, no bartender approval step
- [ ] Bartender only (Stripe off) — price shown, pay-to-bartender flow, no card entry
- [ ] Both on — Stripe path takes precedence per spec; confirm behavior matches intent
- [ ] Both off (auto-approve/free) — no price shown anywhere, requester name still required, request auto-accepted with no approval step
- [ ] Bartender Actions tab shows exactly 2 raw payment toggles (Stripe/Bartender) in the Payments
      card, no phantom 3rd "Auto" toggle mixed in with them — **the separate "Accepting requests"
      toggle in its own Requests card (added 2026-07-18, see §1b) is intentional, not a regression of
      this rule**, don't flag it as one

## 4. requester_name standardization

**Surfaces:** Bartender (LAN x2 + Internet), Customer Up Next (LAN x2 + Internet), kiosk Up Next

- [ ] Submit a request with a name → name displays correctly in Up Next on relay customer.html
- [ ] Same on iOS LAN customer.html
- [ ] Same on Android LAN customer.html (had an explicit fix here — `44e22ab` — double check closely)
- [ ] Name displays correctly on all 3 bartender surfaces (pending + acted cards)
- [ ] Name displays correctly on kiosk-native Up Next (iOS + Android) — also confirm the name entered on a **kiosk-submitted local request** (not just remote web requests) shows correctly everywhere; kiosk local-request forms already use `requesterName` consistently in source (iOS `CatalogBrowseView.swift`, Android `LocalRequestSheet.kt`)
- [ ] Auto-approve mode: name still required and still shows correctly (no regression from the payment-mode refactor)

## 5. Up Next circle badge (tap-to-open)

**Surfaces:** Customer LAN (iOS + Android), Customer Internet, AND kiosk-native (has its own equivalent
implementation, not the same HTML — see below)

- [ ] relay customer.html: badge renders correctly, letters not clipped/tiny, tap opens the queue
- [ ] iOS LAN customer.html: same
- [ ] Android LAN customer.html: same
- [ ] Test on an actual small/older phone browser if possible — this had several follow-up sizing fixes suggesting it was fiddly across browsers
- [ ] Kiosk-native (iOS `KioskView.swift` / Android `KioskView.kt`) already has its own always-visible
      preview strip (row count scales with screen height) plus a tap-to-expand full-queue overlay that
      **self-dismisses after 15s** of no interaction on both platforms (iOS `resetIdleTimer()`, Android
      `delay(15_000)`) — verify live that the dismiss timing actually feels like ~15s and that the
      preview strip's row count looks reasonable on the actual device you test on, not just in source

---

## 6. Genre coloring in native catalog browse

Correction (2026-07-17): earlier project memory said this was still "what needs building" — that was
stale. Verified in source on both platforms: relay `GET /api/bar/{jukebar_id}/genres` (`main.py`) is
live; iOS (`AppState.swift` fetches it into `artistGenres`, `CatalogBrowseView.swift` colors bubbles via
`genreSwiftColor()` and shows long-press tag detail via `TagOverlayCard`); Android (`RelayClient.fetchGenres()`
→ `MainActivity.artistGenres` → `LocalRequestSheet.kt` colors bubbles via `genreColor()` and shows tags
on long-press via `combinedClickable(onLongClick)`). User confirmed working live on iOS already.

**Surfaces:** iOS kiosk catalog browse, Android kiosk/LAN catalog browse (bubble coloring propagates to
`LocalServer.kt` too per source) — this is genuinely native-app-only, no relay-page equivalent to check.

- [x] iOS: bubble colors match playlist genre profile, confirmed working live by user
- [ ] iOS: long-press a bubble → up to 4 raw Last.fm tags shown correctly
- [ ] Android: bubble colors match playlist genre profile (source-verified, not yet live-tested)
- [ ] Android: long-press a bubble → raw tags shown correctly
- [ ] Both: bar that hasn't opted into the map (no GCS profile) shows plain/ungenred bubbles, not an error

---

## 7. Kiosk display mode: Local Only / Local + Remote / Remote Only (2026-07-22)

**Surfaces:** Setup wizard (iOS + Android), Admin kiosk-native + Internet (relay), kiosk Request
button visibility, LAN + relay customer-facing routes

Background: transport (WiFi/Hotspot/Internet) used to be skippable entirely by picking "Local
Only," which silently left admin/bartender unreachable — see CLAUDE.md's "Kiosk display mode is
orthogonal to transport" entry for the full root cause. Also landed same day: Stripe's toggle
shown-but-disabled (not hidden) in Local Only, and a "stale Stripe value" bug class that hit the
kiosk's own Request button — see CLAUDE.md's "Stripe's stored value must never be read raw"
entry. This section covers both together since they were tested/found in the same pass.

- [ ] Pick "Local Only" in the setup wizard on iOS → confirm the network-transport picker
      (WiFi/Hotspot/Internet) still appears, doesn't skip straight to setup
- [ ] Same on Android
- [ ] With Local Only + Internet transport chosen: confirm Admin and Bartender QR codes actually
      render in the kiosk-native Admin screen (`adminURL`/`bartenderURL` resolve, not blank) —
      this was the original reported symptom of the bug
- [ ] With Local Only active on any transport: hit the customer page directly (`/request` on LAN,
      `jukebars.com/bar/{id}` on relay) → confirm a real 404, not a loaded page that then fails to
      submit
- [ ] Same for `POST /api/request` / `/api/create-payment-intent` / `/api/payment-confirmed` /
      `GET /api/request/{id}` (LAN) and their relay equivalents — all should 404/503, not just be
      unreachable via the hidden QR
- [ ] Confirm the kiosk's own on-device Request button still works normally in Local Only (that's
      the one surface that's supposed to keep working)
- [ ] Stripe toggle in Local Only mode: setup wizard AND live Admin screen both show it visibly
      **disabled** (greyed switch) with the "Not usable in Local Only mode" caption — on all of:
      iOS setup + iOS Admin, Android setup + Android Admin, relay `admin.html`, relay
      `bartender.html`
- [ ] **Regression test — the exact bug the user found live:** with Local Only active and Stripe's
      stored value still `true` from before switching modes, turn OFF "Pay to bartender" from the
      Internet-mode admin page (relay `admin.html`) → confirm the kiosk's Request button **stays
      visible** and requests go through **free/auto-accepted**, not hidden as if
      `accepting_requests` were off. Repeat after restarting the host app (confirms the fix
      isn't just a cached-state artifact) — this specific repro is what caught the bug the first
      time
- [ ] Same check on Android
- [ ] With Local Only + Stripe stale-true + Bartender Pay off: confirm the "Both off — free and
      auto-accepted" warning banner **does** show, in the setup wizard AND live Admin, on all
      three platforms — Stripe must count as off for this warning once it's inert, even though its
      stored value is still true
- [ ] Switch a bar OUT of Local Only back to Local + Remote (or Remote Only) → confirm Stripe's
      toggle re-enables and shows whatever value it had before (not silently reset to off) —
      confirms the "raw value preserved, only effective behavior discounted" design actually holds

## 8. Up Next queue-order + Past Requests sort/badge fixes (2026-07-21/22)

**Surfaces:** iOS host (kiosk-native Up Next display + Internet-mode sync payload), relay/LAN
admin.html Reports tab and Requests tab

- [ ] Approve a 2-or-3-song request (bartender-pay or Stripe) → once the first song starts
      playing, confirm it **disappears** from Up Next on the relay customer page within one sync
      tick — it used to keep showing as "up next" until the whole request's last song started
- [ ] Same check on the kiosk's own on-screen Up Next display
- [ ] Past Requests / Reports tab (relay `admin.html`, iOS + Android LAN `admin.html`): confirm
      rows are ordered by when they were actually played/denied (most recent first), not by
      creation time — make a request, let it sit queued a while, then approve/play a *newer*
      request first, and confirm the newer one appears above the older one once both have played
- [ ] Reports tab lists only played/denied rows, not everything still pending/in-queue (already
      covered 2026-07-20, re-check it hasn't regressed)
- [ ] A still-pending bartender-pay request (not yet approved): confirm it shows its price and a
      💵 badge, not "Free" with no price — this was gated on `payment_method`, which stays `"free"`
      until actual approval, so it used to look identical to a genuinely free auto-accepted request
- [ ] A genuinely free/auto-accept request: confirm it shows **no price** and the "Free" badge —
      confirms the companion fix (zeroing price at creation in free mode) didn't overcorrect and
      start showing phantom prices on real free requests
