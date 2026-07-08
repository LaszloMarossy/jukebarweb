# JukeBar — Relay (jukebarweb)

## Spec & docs
All product specs live in `docs/` in this repo:
- `docs/ios_spec.md` — full iOS product behaviour, API, session lifecycle, visual identity
- `docs/android_spec.md` — Android-specific differences (Spotify instead of Apple Music)
- `docs/render_spec.md` — planned discovery/genre-profiling pipeline (Last.fm + recommended playlist)

Architecture decisions belong in this CLAUDE.md, not in the spec files.

---

## What this service does
Render-hosted relay so bars can operate over the internet (not just LAN).
- Host registers at startup, syncs every 5 s
- Customer and bartender browsers poll this service
- Admin browser posts setting changes here; they queue as actions for the host

## Architecture: host is source of truth
The Android/iOS host app owns all state. The relay is a message-passing layer:
1. Host registers with full catalog + bar details
2. Admin/customer/bartender pages read from relay
3. Setting changes from admin → relay queues action → host picks up on next sync → host re-registers with new state

**Optimistic apply:** `POST /api/bar/{id}/settings` now also immediately updates `BarSession` in memory so the admin page doesn't see a stale value in the 0–10 s window before the host re-registers.

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

## Key decisions

**Single currency field** — one ISO currency code for both bartender cash display and Stripe processing. No separate "display currency" vs "Stripe currency".

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
