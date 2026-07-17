# App Store submission checklist — JukeBar (iOS)

Researched 2026-07-15. Designed to be filled out **offline** — bring a text editor, come back
online only to paste into App Store Connect and upload the build/screenshots.

Repo: `~/dev/giffy/JukeBar` · Bundle ID: `giffy.JukeBar` · Device family: iPhone + iPad (`TARGETED_DEVICE_FAMILY = "1,2"`)
— **iPad screenshots are required**, not optional, since the app is built for both.

---

## 0. What's genuinely offline-doable tomorrow vs. needs a connection

**Offline:** draft everything below (metadata text, privacy label answers, App Review notes),
capture screenshots on a simulator/device (no internet needed), check icon/export-compliance facts.
**Needs internet:** actually logging into App Store Connect, uploading the build via Xcode/Transporter,
pasting final text, submitting for review.

---

## 1. Metadata — draft below, char limits from Apple's current spec

| Field | Limit | Draft |
|---|---|---|
| App name | 30 chars | `JukeBar` (7) |
| Subtitle | 30 chars | `Bar Jukebox & Song Requests` (28) — alt: `Play Your Bar's Music, Live` |
| Promotional text | 170 chars | Editable anytime post-launch without a new review — good place for "Now with online Stripe payments!" style updates later. Leave blank or short for v1. |
| Description | 4,000 chars | See draft below |
| Keywords | 100 chars, comma-sep, no spaces, don't repeat title/subtitle words | `jukebox,bar,music,requests,dj,playlist,song,bartender,pub,venue,karaoke,queue` (count chars, trim to fit) |
| Support URL | required | `https://jukebars.com/#contact` |
| Marketing URL | optional | `https://jukebars.com` |
| Category (primary) | — | Music |
| Category (secondary) | — | Utilities |
| Copyright | free text | `© 2026 [your name/entity]` |

**Description draft** (trim/adjust freely — condensed from `static/index.html`):

> JukeBar turns your iPhone or iPad into a self-contained bar jukebox. Play your own Apple Music
> playlist — customers scan a QR code to browse and request songs, no app install needed on their end.
>
> • Works fully offline: playback, requests, and approvals all run on your device, no cloud required
> • Auto-accept free requests, or charge per song / per 3-song bundle
> • Bartenders approve requests from any browser on the same network
> • Optional online mode: let customers and bartenders connect over the internet, not just local WiFi
> • Optional Stripe payments: customers pay online, requests are approved automatically
> • Session reports: every session archives to a CSV you can share via AirDrop or email
> • Optional community map: opt in to list your bar and its music profile so nearby music fans can find you
>
> Music licensing for public performance remains the venue's responsibility, as with any public playback of music.

---

## 2. Age rating questionnaire

Apple overhauled this in 2024–2025: ratings now go 4+ / 9+ / 13+ / 16+ / 18+ (previously topped at 17+),
and the questionnaire added a **"social media / content redistribution"** question. Since this is a new
app, you'll get the current questionnaire automatically — no old-format migration needed.

- Violence, sexual content, profanity, gambling, horror themes → **None** across the board
- Alcohol/tobacco/drug references → JukeBar is bar software; consider **"Infrequent/Mild"** for alcohol
  references (bar context, no depiction/promotion) rather than "None" — judgment call, but honesty here
  avoids a rejection/re-rating later
- Unrestricted web access → **No** (no in-app browser to the open web)
- Social media / redistributes user-generated content to a feed → **No** — the community map shows
  bar/genre info, not a user content feed
- Medical/wellness treatment info → **No**
- Expect a **4+ or 9+** rating

---

## 3. App Privacy "nutrition label"

Apple buckets each declared data type as: **Used to Track You / Linked to You / Not Linked to You.**
Mapped from this app's actual data flows (per `static/index.html` privacy table and `CLAUDE.md`):

| Data type | Collected? | Bucket | Notes |
|---|---|---|---|
| Contact Info (name, email, phone) | No | — | No account/login exists anywhere in the app |
| Location | **Yes, opt-in only** | Not Linked to You *(verify — see below)* | Only sent if "List on map" is enabled in setup; bar name isn't tied to a personal identity, so "Not Linked" is the likely answer, but re-read Apple's Location sub-definitions before submitting — if a reviewer could argue the bar name + GPS re-identifies an individual owner, "Linked" may be the safer pick |
| Financial Info | **Only if Stripe enabled** | Linked to You (if declared) | Card data itself goes straight to Stripe (PaymentSheet), never touches JukeBar's own storage. If the relay stores transaction records (amount + requester name) for reporting, declare that piece as Financial Info / Other Data, linked to the requester name entered for that request |
| Identifiers (device ID, etc.) | No | — | `jukebar_id` is a locally-generated UUID never transmitted except as a relay session key in internet mode — confirm this counts as a "Device ID" under Apple's definition; if so, declare as Identifiers, Not Linked |
| User Content | No | — | No user-generated content beyond a requester's typed name, ephemeral per request |
| Usage Data / Diagnostics | No | — | No analytics or crash reporting SDK in the app |
| Purchases | **Only if Stripe enabled** | — | See Financial Info row above |

**Action for tomorrow:** decide the Location bucket (Linked vs Not Linked) and whether transaction
records persist server-side long enough to count as "collected" — check `main.py` / relay storage for
how long request+payment records live before answering this definitively.

---

## 4. Screenshots

Apple's current required sizes (auto-scales down to smaller device classes if you only supply the largest):

| Device class | Required? | Pixel size (portrait) |
|---|---|---|
| iPhone 6.9" (17/16 Pro Max, 16 Plus, 15 Pro Max/Plus, 14 Pro Max) | **Required** | 1320 × 2868 |
| iPad 13" (Pro M4/M5, Air M2–M4) | **Required** (app supports iPad) | 2064 × 2752 |

1–10 images per device class, PNG/JPG, **no alpha/transparency**. Supplying just these two largest
sizes lets App Store Connect auto-generate the rest (6.5"/6.3"/6.1"/5.5" iPhone, 12.9"/11"/10.5"/9.7" iPad).

**Suggested shot list** (6 works well, covers the product without padding):
1. Kiosk/admin home screen — now playing + QR code visible
2. Setup wizard — playlist picker or network-mode step
3. Customer web view (Safari, same device or second phone) — catalog browse
4. Customer web view — request/payment sheet (Stripe or free-request confirmation)
5. Bartender web view — pending requests / approve-deny screen
6. Admin controls panel — playback + approval-mode toggle

Capture all of these on the **iPhone 6.9" simulator** and the **iPad 13" (or nearest) simulator** —
fully doable offline, no device needed if the simulator has a downloaded/local Apple Music track to
demo with (otherwise plan to grab real-device shots once you're back near a bar/test playlist).

---

## 5. App icon

1024×1024 PNG, **no alpha channel**, no pre-rounded corners (Apple applies the mask). Check the current
JukeBar app icon asset meets this — if it was exported with transparency it will bounce at upload.

---

## 6. Build / export compliance

- **ITSAppUsesNonExemptEncryption → `false`.** Standard HTTPS/TLS (URLSession), Keychain, StoreKit are
  all exempt under 15 CFR 740.17 ("standard OS encryption"). JukeBar doesn't implement custom crypto,
  so this is a one-time checkbox, no export license needed.
- Xcode/iOS SDK minimum version isn't a metadata field — Xcode enforces the current minimum automatically
  at archive time. Just make sure Xcode itself is updated before archiving.

---

## 7. Payment policy (IAP vs. external/Stripe) — lower risk than it looks

Apple's Guideline 3.1.1 requires In-App Purchase only for **digital content consumed inside the app**
(subscriptions, unlockable digital features). **Physical goods and real-world services are explicitly
exempt** — JukeBar's Stripe charge pays for a song played on physical bar equipment, the same exemption
category as Uber, DoorDash, or OpenTable. Apple's IAP should not be required here.

Additionally, a 2025 US court ruling (Epic Games case) removed the requirement for a special "External
Purchase Link Entitlement" for apps on the **US storefront** specifically — external payment links/flows
are now unrestricted for US apps. Net: Stripe-as-is should be fine, but add one clarifying line to the
App Review notes anyway (see below) since reviewers sometimes flag payment flows for a manual look
regardless of the underlying policy.

---

## 8. App Review Information

- Contact info: your name/phone/email
- **Demo account fields: leave blank** — JukeBar has no login. Use the Notes field instead (Apple's own
  guidance: explain non-standard setups there rather than leaving reviewers to guess)
- **Draft Notes field text:**

  > JukeBar has no login or account system. The fastest way to review is Standalone mode: on first
  > launch, complete the setup wizard (pick any downloaded Apple Music playlist), choose "Auto-accept"
  > and "Local only" network mode — this requires no WiFi setup and lets you make requests directly on
  > the kiosk screen. The app also runs an embedded local HTTP server (NSLocalNetworkUsageDescription)
  > so customers/bartenders on the same WiFi can connect via browser — this is optional and not required
  > to exercise the core playback/request flow. Payments (optional, via Stripe) are for a real-world
  > service — a song played at a physical bar — not digital content, consistent with guideline 3.1.1's
  > physical-goods/services exemption. [Add Stripe test-mode card details here if Stripe is enabled for
  > the reviewed build.]

---

## 9. Gotchas specific to this app

- **NSLocalNetworkUsageDescription** must be specific, not boilerplate — current string in `Info.plist`
  ("JukeBar runs a local server so customers and bartenders can connect from their phones on the same
  network.") is good, keep it.
- **NSLocationWhenInUseUsageDescription** current string ("JukeBar reads your Wi-Fi network name...")
  describes the WiFi-SSID-display use, but the *same* location permission also feeds the community map's
  GPS opt-in — make sure the string (or a second, more specific one if iOS lets you split it) also covers
  the map use case, since a privacy-label / permission-prompt mismatch is a common rejection trigger.
- **Internet-mode reviewer risk:** if you demo internet mode instead of standalone, `jukebars.com` (the
  relay) must be up and reachable for the entire review window — prefer steering reviewers to Standalone
  mode per the Notes draft above to remove that dependency entirely.

---

## Not yet resolved — decide before submitting (not blocking for tomorrow's offline draft work)

- [ ] Location privacy-label bucket: Linked vs Not Linked to You (see §3)
- [ ] Whether relay-stored transaction/request records count as "collected" Financial Info (see §3)
- [ ] Confirm current app icon asset has no alpha channel (see §5)
- [ ] Confirm `jukebar_id` UUID transmission in internet mode doesn't need an Identifiers declaration
