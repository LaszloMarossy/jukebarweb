# JukeBar Web — Spec

## Playlist Genre Profiling, Map Visualisation & Recommended Playlist

> Note: Spotify deprecated genre tags and recommendations for new apps in Nov 2024.
> **Last.fm API** is used instead — free, stable, no user auth required.

---

### Data source: Last.fm API

Two endpoints cover everything needed:

- `artist.getTopTags` — community-voted genre tags for any artist, weighted by tag count
- `artist.getSimilar` — similar artists ranked by similarity score

No user login required. Free API key from last.fm/api/account/create.

Fallback for obscure artists not in Last.fm: LLM (Claude/GPT) classification by artist name.

---

### Pipeline 1 — Genre Profile & Map Pie Chart

**Input:** `[{name, song_count}]` from the bar's playlist — **implemented June 2026**.
iOS (`AppState.swift`) and Android (`RelayClient.kt`) now count songs per artist and send `[{name, song_count}]`.
Relay accepts both legacy `[str]` and new `[{name, song_count}]` for backward compat.
Daemon unpacks counts and uses them as weights throughout the pipeline.

**Step 1 — Tag each artist**
Call `artist.getTopTags` for each band. Returns tags like `["heavy metal", "thrash metal", "hard rock"]`
with a community weight per tag.

**Step 2 — Weight by song count** ✓ implemented
Multiply tag weights by the band's song count in the playlist.
A band with 20 songs contributes 20× more to the pie than a one-off track.
Result: a weighted dictionary of the bar's true genre identity.

**Step 3 — Map to top-level genre palette**
Collapse Last.fm's micro-tags into our fixed colour-coded top-level genres:

| Genre | Colour | Example Last.fm tags |
|-------|--------|----------------------|
| Heavy metal | Black | heavy metal, thrash metal, death metal, doom metal |
| Punk | Red | punk, hardcore punk, post-punk, emo |
| Electronic / EDM | Cyan | electronic, techno, house, drum and bass, ambient |
| Jazz | Gold | jazz, bebop, fusion, jazz blues |
| Classical | Ivory | classical, baroque, orchestral, opera |
| Hip-hop | Orange | hip-hop, rap, trap, r&b |
| Folk / Acoustic | Green | folk, acoustic, singer-songwriter, country |
| Rock | Blue | rock, alternative rock, indie rock, grunge |
| Pop | Pink | pop, synth-pop, dream pop, indie pop |
| World / Other | Brown | world music, reggae, latin, flamenco |

**Step 4 — Store to GCS**
Write `map/{jukebar_id}/profile.json`: weighted top-level genre breakdown.
The background Render worker does this — never inline with requests.

**Visual output on the map:**
- Map marker = SVG pie/donut chart (Leaflet DivIcon, no extra library) showing genre mix
- Band bubbles in the slide-up panel coloured by primary genre
- Glanceable at city level: dark clusters = metal bars, cyan = electronic, etc.

---

### Pipeline 2 — Recommended Playlist Construction

**Input:** same playlist artist list

**Step 1 — Find similar artists**
Call `artist.getSimilar` for each band in the playlist, weighted by song count.
Aggregate and rank similar artists across the whole playlist.
Filter out artists already in the playlist → ranked list of recommended new artists.

**Step 2 — Look up on Apple Music / Spotify**
For each recommended artist, search Apple Music (MusicKit) or Spotify to find actual
playable tracks. Build a concrete recommended playlist of real songs.

**Step 3 — Surface the recommended playlist**

Two use cases:

**A) JukeBar — Recommended Playlist Mode**
In the JukeBar iOS app, offer the bar owner a choice at session start:
- *Play your own playlist* (current behaviour)
- *Play the recommended playlist* (Last.fm-derived, looked up on Apple Music)

In recommended mode, the jukebox rotates from the constructed playlist.
Customers can still browse and request from it. Everything else (approval, QR, bartender) works identically.

**B) TuneTaster — Personal Discovery App**
TuneTaster (separate app, in planning) uses the same pipeline for personal use:
a user feeds in their favourite artists → gets a recommended playlist of similar music
they haven't heard → can play it directly on Apple Music or Spotify.

---

### Architecture: Platform-Neutral Render Worker + Per-Platform Resolution

**Render does only what is platform-agnostic:**
- Receives `[{artist, song_count}]` on `POST /api/map/register`
- Background worker calls Last.fm `artist.getSimilar` per artist (weighted by song count)
- Aggregates → ranked list of recommended artist *names* (plain strings, no music platform IDs)
- Writes to GCS: `map/{jukebar_id}/recommendation.json` — just artist names
- Also writes `map/{jukebar_id}/profile.json` — genre breakdown for the map pie chart
- Render never touches Apple Music or Spotify — no platform credentials needed server-side

**Per-platform resolution happens on-device (background task after session start):**

| Platform | App | Resolution |
|----------|-----|------------|
| iOS | JukeBar (Apple Music) | Downloads artist list from GCS → MusicKit catalog search → builds playable queue |
| Android | spotonjukebar (Spotify) | Downloads artist list from GCS → Spotify Android SDK search → builds Spotify playlist |

On next restart, if resolved track IDs are already cached on-device (or pushed back to GCS),
the search step is skipped and the playlist loads directly.

**Data flow:**
```
Render worker:
  Last.fm similar artists → [{artist_name}] → GCS (platform-neutral)

iOS background task:
  GCS artist list → MusicKit search → Apple Music track IDs → playable queue

Android background task:
  GCS artist list → Spotify SDK search → Spotify track URIs → Spotify playlist
```

---

### Client changes required

**iOS (JukeBar):**
- `POST /api/map/register` payload: `artists: [String]` → `artists: [{name: String, song_count: Int}]`
- On session start: check GCS for available recommended playlist for this bar
- If available: offer "Play own playlist" vs "Play recommended playlist" toggle
- Background task: resolve artist names via MusicKit, build ApplicationMusicPlayer queue

**Android (spotonjukebar):**
- Same `POST /api/map/register` change
- Background task: resolve artist names via Spotify Android SDK search
- Build and queue a Spotify playlist from the results

---

### Dependencies
- Last.fm API key (free) — server-side only
- `pylast` Python library
- MusicKit (iOS, existing) — on-device only
- Spotify Android SDK (existing in spotonjukebar) — on-device only
- No Spotify Web API or Apple Music server-side credentials needed
