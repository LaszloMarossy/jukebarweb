# JukeBar Web — Spec

## Planned: Playlist Genre Profiling & Spotify-Based Recommendations

### Context
The map/discover page shows registered JukeBars with their artist bubbles. A future enhancement
is to derive a genre profile from the bar's playlist and use it to surface recommendations or
characterise the bar's music identity on the discover page.

### The Programmatic Approach (raw playlist data → Spotify API)

Given a list of `[Band Name, Number of Songs]` from the bar's Apple Music playlist:

**Step 1 — Map Artists to Genres**
Feed band names into Spotify's artist search/lookup API (via `spotipy`).
Spotify stores specific genre tags per artist (e.g. Radiohead → `['alternative rock', 'art rock', 'melancholia']`).

**Step 2 — Weight the Profile**
Multiply each genre's count by the number of songs that band contributes to the playlist.
If Band A has 10 songs and Band B has 2 songs, Band A's genres carry 5× the weight.
Result: a weighted dictionary representing the playlist's true genre identity.

**Step 3 — Generate Recommendations**
Pass the top 3 weighted genres or top 5 most frequent artists as "seeds" into Spotify's
recommendations API endpoint. Returns a fresh list of mathematically similar tracks
the bar hasn't played yet.

### Architecture: Background Render Job

The profiling runs as a **Render background worker** (or cron job), not inline with requests:

- On each `POST /api/map/register`, the bar's `artists[]` and song counts are stored as-is.
- The background job works through registered bars at a leisurely pace, calling the Spotify API
  to build the weighted genre profile for each bar's playlist.
- The resulting profile (top genres, style tags, mood descriptors) is written to GCS
  (e.g. `map/{jukebar_id}/profile.json`).
- The discover map reads only from GCS — it never touches the Spotify API.
  All enrichment happens exclusively in the background worker.

This keeps registration fast, avoids Spotify rate-limit pressure, and means the map enriches
itself gradually over time as bars register and the worker runs.

### Visual Output on the Map

**Genre colour palette** — each top-level genre gets a fixed colour, e.g.:
- Heavy metal → black
- Punk → red
- Electronic / EDM → cyan
- Jazz → gold
- Classical → ivory
- Hip-hop → orange
- Folk / Acoustic → green
- Pop → pink
- (full palette TBD)

**Band bubbles** — artist tags in the slide-up panel are coloured by their primary genre,
using the same palette.

**Map marker = pie chart** — the map pin for each bar is rendered as a small pie/donut chart
showing the proportional genre mix of that bar's playlist (e.g. 60% metal / 25% punk / 15% rock).
At a glance, a user browsing the map can see what kind of place each bar is before opening the panel.

Implementation note: pie chart markers can be rendered as SVG data URIs used as Leaflet
`DivIcon`s — no canvas or extra library needed.

### Integration Points
- The iOS app already sends `artists[]` in `POST /api/map/register` — this is the seed data.
- Song-count weighting requires the iOS app to send `[{artist, song_count}]` instead of just `artists[]` (small change).
- Recommendations could be surfaced in the admin panel as a "you might also want to add" list.

### Dependencies
- `spotipy` Python library
- Spotify Developer app credentials (client_id, client_secret)
- Bar owner opt-in (Spotify data lookup is based on their playlist's artist names, no Spotify account required from the bar)
