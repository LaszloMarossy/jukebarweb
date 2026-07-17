# Testing checklist — features since 2026-06-26

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

Coverage matrix (which surfaces each feature touches):

| Feature | A-K-i | A-K-a | A-L-i | A-L-a | A-N | B-L-i | B-L-a | B-N | C-K-i | C-K-a | C-L-i | C-L-a | C-N |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1. End Session / shutdown | X | X | | | | | | | | | | | |
| 1b. accepting_requests block | | | | | | | | | X | X | X | X | X |
| 2. Versioned settings lock | | | X | X | X | X | X | X | | | | | |
| 3. Payment-mode raw flags | X | X | X | X | X | X | X | X | X | X | X | X | X |
| 4. requester_name | | | | | | X | X | X | X | X | X | X | X |
| 5. Up Next circle badge | | | | | | | | | X | X | X | X | X |

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

**Surfaces:** Customer LAN (iOS + Android), Customer Internet/relay; verify kiosk-native too

- [ ] Toggle off accepting new requests → Browse/Pay/Submit buttons disappear, "Not accepting requests right now" shown within ~5s poll (relay customer.html)
- [ ] Same on iOS LAN customer.html
- [ ] Same on Android LAN customer.html
- [ ] QR code + now-playing stay visible throughout (not hidden along with the buttons)
- [ ] `/request` and `/create-payment-intent` return 403 while off
- [ ] `/payment-confirmed` still succeeds for a request already in flight when the flag flips off
- [ ] Kiosk-native (iOS `KioskView.swift`'s `allowsLocalRequest`, Android `KioskView.kt`'s `showLocalRequestButton`) already gates the local Request button on this flag in source — verify live: toggle off, confirm the kiosk still shows now-playing/QR but hides/disables its own Request button too

## 2. Versioned settings confirmation ("trickle-back")

**Surfaces:** Admin LAN (iOS + Android), Admin Internet, Bartender LAN (iOS + Android), Bartender Internet

- [ ] Flip a payment toggle on relay `admin.html` → toggle visibly locks (dimmed, unclickable) instantly
- [ ] A second browser tab on relay `admin.html` for the same bar also shows the toggle locked, not just the tab that clicked it
- [ ] relay `bartender.html` for the same bar also shows the lock
- [ ] Toggle unlocks within ~5s (one host sync cycle) once host picks it up and echoes back
- [ ] Repeat on iOS LAN admin.html / bartender.html
- [ ] Repeat on Android LAN admin.html / bartender.html
- [ ] Rapid double-toggle (change, then change again before the first confirms) → confirm last-one-wins, no permanently-stuck-locked state
- [ ] Kill host mid-sync (simulate dropped sync) → confirm toggle self-heals on next 5s heartbeat rather than staying locked forever

## 3. Payment-mode refactor (raw stripeEnabled/bartenderEnabled flags)

**Surfaces:** effectively all 13 — prioritize the 4x4 combo grid below over exhaustive per-surface passes

Run these 4 combinations, each checked on: relay admin+bartender+customer, iOS LAN admin+bartender+customer, iOS kiosk request sheet, Android LAN admin+bartender+customer, Android kiosk request sheet:

- [ ] Stripe only (bartender off) — price shown, payment required, no bartender approval step
- [ ] Bartender only (Stripe off) — price shown, pay-to-bartender flow, no card entry
- [ ] Both on — Stripe path takes precedence per spec; confirm behavior matches intent
- [ ] Both off (auto-approve/free) — no price shown anywhere, requester name still required, request auto-accepted with no approval step
- [ ] Bartender Actions tab shows exactly 2 raw toggles (Stripe/Bartender), no phantom 3rd "Auto" toggle, on all 3 bartender surfaces

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
