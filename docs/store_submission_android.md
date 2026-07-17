# Google Play submission checklist — Spot On! JukeBar (Android)

Researched 2026-07-15. Designed to be filled out **offline** — bring a text editor, come back
online only to enroll in Play App Signing, create the closed-test track, and upload the build.

Repo: `~/dev/giffy/spotonjukebar` · Package: `com.giffy.spotonjukebar` · `compileSdk`/`targetSdk` = 36
(Android 16) — **already meets** Google's Aug 31, 2026 target-API deadline, no action needed there.

---

## 0. THE TWO TIME-CRITICAL ITEMS — start these the moment you're back online

**A. Closed testing track (12 testers, 14 continuous days) — this is a first-submission requirement
for new personal developer accounts and gates Production entirely.** It doesn't need to be your last
step — it should be closer to your first, since nothing else you do shortens the 14-day clock.
1. Create a Google Play Console account if you haven't (one-time $25 fee)
2. Build a release `.aab`, upload it to a **Closed testing** track
3. Recruit ≥12 people (friends, family, other bar contacts) to opt in via the tester link and open the app
4. Keep ≥12 opted-in for 14 continuous days — dropping below 12 at any point resets the clock
5. Only after that window does "Production" unlock in Console

**B. 16KB native-library page-size check** — Google made this a hard upload blocker on May 1, 2026 for
apps with native (`.so`) libraries. Checked this repo already:
- Spotify SDK is pulled in as pure AARs (`spotify-app-remote-release-0.8.0.aar`,
  `spotify-auth-release-2.1.0.aar`) — no native code, not a risk.
- The only `.so` found in build output is `libandroidx.graphics.path.so`, a transitive Jetpack Compose
  dependency, not your own code. Your `compose-bom` is pinned to `2024.09.00` (Sept 2024), which may
  predate the 16KB alignment fix in that specific artifact; AGP 8.11.0 / Gradle 8.13 are recent enough
  to build 16KB-aligned by default.
- **Offline-doable verification:** build the release `.aab`, extract it, and run
  `zipalign -c -v 16 <path-to-apk-or-aab-contents>` against the extracted native libs to confirm
  alignment before you ever touch Play Console. If it flags anything, bump `composeBom` in
  `gradle/libs.versions.toml` to a 2025+ release — that's the likely one-line fix.

---

## 1. Store listing fields

| Field | Limit | Draft |
|---|---|---|
| Title | 30 chars | `Spot On! JukeBar` (17) |
| Short description | 80 chars | `Bar jukebox: play Spotify or MP3s, let customers request songs by QR code` (75) |
| Full description | 4,000 chars | See draft below |
| Category | — | Music & Audio |
| Contact email | required | `marossy@gmail.com` |
| Contact phone/website | optional | `https://jukebars.com` |
| Privacy policy URL | **required**, must be live/public | `https://jukebars.com/#privacy` |

**Full description draft** (condensed from `static/index.html`, adjust for Spotify-vs-Apple-Music wording):

> Spot On! JukeBar turns your Android phone or tablet into a self-contained bar jukebox. Play a
> Spotify playlist or your own uploaded MP3s — customers scan a QR code to browse and request songs,
> no app install needed on their end.
>
> • Works fully offline with local MP3s; Spotify streaming needs internet + subscription
> • Auto-accept free requests, or charge per song / per 3-song bundle
> • Bartenders approve requests from any browser on the same network
> • Optional online mode: connect over the internet, not just local WiFi
> • Optional Stripe payments: customers pay online, requests are approved automatically
> • Optional community map: opt in to list your bar and its music profile so nearby music fans can find you
>
> Music licensing for public performance remains the venue's responsibility, as with any public playback of music.

---

## 2. Graphic assets

| Asset | Spec |
|---|---|
| App icon | 512×512 PNG, 32-bit with alpha |
| Feature graphic | 1024×500, JPG or 24-bit PNG (no alpha) |
| Phone screenshots | 2–8 images, JPEG/24-bit PNG, no alpha, min 320px/max 3840px per side, longest side ≤ 2× shortest |
| Tablet screenshots (7"/10") | Optional in 2026, but if the manifest declares large-screen support, **≥4 per tier** earns the "Tablet-optimized" badge + search boost. 7" ≈ 1200×1920, 10" ≈ 1600×2560 |

**Suggested phone shot list** (mirrors the iOS list for consistency across store listings):
1. Kiosk home screen — now playing + QR code
2. Setup wizard step (Spotify/local-folder picker)
3. Customer request sheet — catalog browse
4. Customer request sheet — payment/confirmation screen
5. Bartender web view — pending requests / approve-deny
6. Admin screen — playback + approval-mode toggle

All capturable offline via emulator/device screenshot, same as the iOS set.

---

## 3. Data safety section

Mapped from `AndroidManifest.xml`'s actual declared permissions:

| Permission / data type | Collected? | Purpose | Notes |
|---|---|---|---|
| `ACCESS_FINE_LOCATION` / `ACCESS_COARSE_LOCATION` | **Yes, opt-in only** | App functionality | Only sent when "List on map" is enabled; encrypted in transit (HTTPS); removable via map opt-out |
| `READ_MEDIA_AUDIO` / `READ_EXTERNAL_STORAGE` | No (device-local only) | — | Used to read local MP3 files for the catalog; nothing leaves the device |
| `ACCESS_WIFI_STATE` | No | — | Used only to display network name on-screen, not stored/transmitted |
| Financial info (Stripe payments) | **Only if Stripe enabled**, processed by Stripe | App functionality | Card data collected by Stripe's SDK directly, not by the app; declare that transaction data is processed via a third party (Stripe) |
| Device/other IDs | No | — | No analytics/crash SDK present; double-check the Spotify SDK doesn't silently read Android ID — if it does, must be declared |
| App activity / analytics | No | — | No analytics SDK in this repo |

**Also required:** a data-deletion mechanism or explanation. Since almost everything is device-local,
draft language: *"Bar configuration and catalog data are stored only on the host device and are
deleted when the app is uninstalled. Location data submitted to the community map can be removed at
any time by disabling 'List on map' in setup, which unpublishes the bar's map entry."*

---

## 4. Content rating (IARC) questionnaire

One questionnaire generates ratings across ESRB/PEGI/USK/etc. simultaneously.

- Violence, sexual content, gambling simulation → **None**
- **"Shares user's precise location with other users"** → **Yes** — the community map publishes bar
  GPS location publicly; must be declared as an interactive element
- **Unrestricted internet access** → **Yes**
- **Digital purchases** → **Yes** (Stripe payments, even though off Play Billing — the questionnaire
  still asks whether the app facilitates purchases at all)
- Note: Google required all developers to re-answer updated age-rating questions by Jan 31, 2026
  (already passed) — as a new submission you'll just get the current questionnaire fresh, no migration
  concern.

---

## 5. Target audience / Ads / Financial features declarations

- **Target audience:** not primarily designed for children — bar/nightlife context keeps Play Families
  policies out of scope
- **Ads:** No ads (confirm no ad SDK present — none found in this repo)
- **Financial features declaration:** this is a screening question about apps whose *core* feature is
  money management (loans, crypto, money transfer, etc.). JukeBar merely takes payment via Stripe for a
  real-world service — same category as any e-commerce/food-delivery app — expect to answer **No** to
  this declaration, but read the actual Console question text since Google's definitions shift over time

---

## 6. Other App content declarations

News apps, COVID-19 apps, government apps — all **not applicable**, answer No/skip.

---

## 7. Technical/policy requirements

| Requirement | Status |
|---|---|
| Target API level (Android 16 / API 36) by Aug 31, 2026 | **Already met** — `compileSdk`/`targetSdk` = 36 |
| 16 KB native-library alignment (hard blocker since May 1, 2026) | Likely fine — verify per §0.B above |
| Android App Bundle (`.aab`) required, `.apk` no longer accepted for production | Confirm your build pipeline outputs `.aab` |
| Play App Signing enrollment | Effectively mandatory for new apps — enroll when you first upload |

---

## 8. Payment policy (Play Billing vs. Stripe) — Play Billing correctly NOT required

Google's payments policy exempts "purchase of physical services... tickets for live events" and
similar real-world-consumption purchases from requiring Google Play Billing. A song request fulfilled
and played at a physical bar sits squarely in that exemption bucket — same category as food delivery,
ride-hailing, or event-ticketing apps. Stripe-as-is should be fine; just be ready to explain the
exemption clearly if Play Console's payments questionnaire flags it for manual review (a one-line note
citing "real-world service consumed at a physical venue, not digital content" is the right framing).

---

## 9. Gotchas specific to this app

- **Privacy policy / Data safety form must match** — any mismatch between the `jukebars.com` privacy
  page content and what's declared in the Data safety form is a documented common rejection/appeal
  trigger. Since both draw from the same source material (`static/index.html`'s privacy table), keep
  them in sync if either changes.
- **Location permission for an opt-in feature:** must not be requested at first launch — only when the
  user reaches the "List on map" setting in setup. Confirm the actual permission-request timing in
  `SetupWizardScreen.kt` / relevant setup step matches this before submitting.
- **Companion backend relay (jukebarweb):** not itself reviewed, but if internet mode is demoed to
  reviewers it must be live/reachable for the review window — same consideration as the iOS side.

---

## Not yet resolved — decide before submitting (not blocking for tomorrow's offline draft work)

- [ ] Run the `zipalign -c -v 16` check on a release build (§0.B)
- [ ] Confirm Spotify SDK doesn't silently collect Android ID (§3)
- [ ] Confirm location permission isn't requested before the user reaches the map opt-in setting (§9)
- [ ] Confirm build pipeline outputs `.aab`, not `.apk`, for the Play Console upload
- [ ] Recruit the 12 closed-test testers — start this list now, it's the long pole
