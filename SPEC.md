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

**Input:** `[{artist, song_count}]` from the bar's Apple Music playlist
(iOS app change needed: send song counts alongside artist names in `POST /api/map/register`)

**Step 1 — Tag each artist**
Call `artist.getTopTags` for each band. Returns tags like `["heavy metal", "thrash metal", "hard rock"]`
with a community weight per tag.

**Step 2 — Weight by song count**
Multiply tag weights by the band's song count in the playlist.
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

### Architecture: Background Render Worker

- `POST /api/map/register` stores raw `[{artist, song_count}]` immediately, returns fast.
- Background worker iterates registered bars, calls Last.fm, builds profiles, writes to GCS.
- Discover map and iOS app read only from GCS — Last.fm is never hit at request time.
- Worker runs on a schedule (Render cron) or as a persistent background process.

---

### iOS changes required
- `POST /api/map/register` payload: change `artists: [String]` → `artists: [{name: String, song_count: Int}]`
- New session-start UI: "Play own playlist" vs "Play recommended playlist" toggle
- MusicKit search to resolve Last.fm-recommended artist names to playable Apple Music tracks

---

### Dependencies
- Last.fm API key (free)
- `pylast` Python library (Last.fm client)
- MusicKit (iOS, existing) or Spotify iOS SDK for track lookup
- No Spotify Web API dependency (genre/recommendations deprecated for new apps Nov 2024)
