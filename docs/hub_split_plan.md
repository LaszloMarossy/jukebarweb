# Documentation hub split — index.html → hub + ios.html + android.html

Plan for splitting the single `static/index.html` into a platform-neutral hub plus two
platform-specific pages. **I have not touched `index.html`** since you're actively hand-editing it —
this plan and the two new skeleton files are additive only.

## Why split

`index.html` currently mixes neutral content (what JukeBar is, pricing model, privacy table, contact)
with iOS-specific detail written as if iOS were the only platform (Apple Music, "Personal Hotspot",
AirDrop reports) — Android has no equivalent page, and its setup flow genuinely differs (Spotify auth +
playlist picker, or a local-MP3-folder picker, rather than "authorize Apple Music").

## New routes (added to `main.py`)

```python
@app.get("/ios", response_class=HTMLResponse)
async def ios_docs():
    return (Path("static") / "ios.html").read_text(encoding="utf-8")


@app.get("/android", response_class=HTMLResponse)
async def android_docs():
    return (Path("static") / "android.html").read_text(encoding="utf-8")
```

Same pattern as the existing `/discover` route. Not wired up yet — see bottom of this file.

## Section-by-section mapping

| Section | Stays in hub (`index.html`)? | Moves to `ios.html` / `android.html`? |
|---|---|---|
| Header / nav | Yes | Yes (own copy) — nav gets "← All platforms" back-link + link to the sibling platform page |
| Hero badges (offline/online, 3 modes, free/charge, payments, community) | **Yes, unchanged** — already written platform-neutrally | — |
| Feature grid — "🎵 iOS device plays your music" card | Genericize to a neutral card ("Your device plays your music — Apple Music on iOS, Spotify/local files on Android") with a link out | Each platform page repeats the grid with its own accurate first card |
| Feature grid — remaining cards (modes, approval, payments, reports, community) | **Yes, unchanged** | — |
| How it works — bar owner role card | Genericize the "Apple Music playlist" line, link to platform guide for exact steps | Full accurate step list per platform |
| How it works — customer / bartender role cards | **Yes, unchanged** (already neutral) | — |
| **Setup guide** (wizard steps, downloading songs, bartender pairing) | Trim to a 2-line teaser + links out | **Moves entirely, needs a rewrite** — see warning below |
| **Network setup** (Shared WiFi, Hotspot, Local only, Internet mode) | **Mostly stays** — concept is identical on both platforms | Only "Option B — Hotspot" needs an aside noting Android's different settings-menu naming ("Wi-Fi hotspot & tethering" vs iOS "Personal Hotspot") |
| FAQ — network/PIN/reports/multi-bartender/offline/restart questions | **Yes, unchanged** — genuinely platform-neutral | — |
| FAQ — Apple Music download/cloud-icon question | Remove from hub | → `ios.html` |
| FAQ — Spotify device-transfer / local-folder equivalents | — | → `android.html`, **new content, doesn't exist yet** |
| Privacy table + community programme + data retention tips | **Yes, unchanged** | — |
| Privacy — "Apple Music" subsection | Remove from hub | → `ios.html` |
| Privacy — Spotify/local-files equivalent | — | → `android.html`, **new content** |
| Privacy — "Local network" subsection | **Yes, unchanged** | — |
| Contact | **Yes, unchanged** | — |

## ⚠️ Known-stale content — do not copy forward as-is

The current `#setup` section's "Set QR expiry" step describes `qrExpiryHours` / session-expiry, which
was **removed** from the iOS app per `~/dev/giffy/JukeBar` commit `b674111` ("Remove qrExpiryHours /
sessionExpiry — replaced by Stop button + pause timer"). The setup-guide content on the live site is
already out of date for iOS. When writing `ios.html`'s setup section, verify the current wizard steps
against `JukeBar/AdminView.swift` and the setup wizard views directly — don't just copy-edit the old
prose. I didn't rewrite this myself since it needs a source-of-truth check against current Swift code,
not just a wording pass.

Android's setup section is **new content, not a copy-edit** — per memory of the wizard's actual steps:
Spotify auth (PKCE/Chrome Custom Tabs) or local-MP3-folder picker (`LocalFolderStep`), bar details +
PIN (`BarDetailsStep`), Spotify device selection (`SpotifyDeviceStep`), playlist picker
(`SpotifyPlaylistStep`). Verify against `~/dev/giffy/spotonjukebar/ui/setup/*Step.kt` before writing.

## Update — content written, routes live (2026-07-15)

`static/ios.html` and `static/android.html` now have full draft content, not stubs — setup guide,
network setup, FAQ, and privacy sections all written out. The wizard step-by-step content in both
was verified directly against current source (`SetupView.swift` / `DisplayModeGateView.swift` /
`NetworkGateView.swift` for iOS; `SetupWizardScreen.kt` + `ui/setup/*Step.kt` for Android) rather than
copy-edited from the old stale `index.html` prose — this fixed the QR-expiry staleness flagged above;
that step no longer exists in either app's actual wizard and isn't mentioned on either new page.

The `/ios` and `/android` routes are now wired up in `main.py` (same pattern as `/discover`).

**Still worth your own pass on the train**, since I drafted this without running either app live:
- Both pages have a short "Good fit for" positioning blurb near the top — you know the actual
  differentiation angle (Spotify vs Apple Music, pricing, etc.) better than a draft can.
- FAQ sections are thin (3 items each) — add real support-inbox questions as they come in.
- iOS "uploading" step (the spinner after Finish) wasn't given user-facing copy since there's nothing
  to document beyond "the app starts" — confirm nothing else happens there worth mentioning.
- Neither page's nav highlights which section you're on — cosmetic, low priority.

## Suggested nav addition for `index.html` (paste in yourself alongside your manual edits)

```html
<a href="/ios">🍎 iOS guide</a>
<a href="/android">🤖 Android guide</a>
```
