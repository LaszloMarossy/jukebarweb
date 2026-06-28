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

---

## MacLord — Genre Profiling Daemon Setup

MacLord is a always-on Mac (macOS Catalina 10.15) on the local LAN that runs `profile_daemon.py`
as a background LaunchAgent. It polls GCS every 30 s, profiles stale bar playlists via Last.fm,
and writes results back to GCS.

**Machine details**
- Local IP: `192.168.0.108`
- Python: `/usr/local/bin/python3`
- GCS key: `/Users/laszlo/gcs-key.json`
- Repo clone: `~/jukebarweb/`

---

### LaunchAgent plist

File: `~/Library/LaunchAgents/com.jukebar.profiler.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.jukebar.profiler</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-is</string>
    <string>/usr/local/bin/python3</string>
    <string>/Users/laszlo/jukebarweb/profile_daemon.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>GCS_BUCKET</key>
    <string>jukebar-data</string>
    <key>GOOGLE_APPLICATION_CREDENTIALS</key>
    <string>/Users/laszlo/gcs-key.json</string>
    <key>LASTFM_API_KEY</key>
    <string>91af5134238b343dc80bece72905780a</string>
    <key>PROFILE_POLL_INTERVAL</key>
    <string>30</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>/Users/laszlo/jukebarweb</string>
  <key>StandardOutPath</key>
  <string>/Users/laszlo/jukebarweb/daemon.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/laszlo/jukebarweb/daemon.log</string>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
```

`caffeinate -is` wraps the daemon so MacLord stays awake with the lid closed.
`KeepAlive: true` means launchd auto-restarts it if it crashes.

---

### Install / update commands

```bash
# First install
cp com.jukebar.profiler.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.jukebar.profiler.plist

# After editing the plist or pulling a new daemon version
launchctl unload ~/Library/LaunchAgents/com.jukebar.profiler.plist
cd ~/jukebarweb && git pull
launchctl load ~/Library/LaunchAgents/com.jukebar.profiler.plist

# Check it's running (shows PID and exit code)
launchctl list | grep jukebar

# Tail the log
tail -f ~/jukebarweb/daemon.log
```

Shortcut — `~/jukebarweb/restart_daemon.sh` does the unload/pull/load sequence.

---

### macOS server hardening (run once, prevents auto-update chaos)

macOS will schedule OS updates and restart the machine automatically unless disabled.
After a restart, it restores every app that was open ("window state restoration"), causing
heat, lag, and an unusable desktop. Disable both permanently:

```bash
# Disable all automatic OS/app store updates
sudo softwareupdate --schedule off
sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticCheckEnabled -bool false
sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticDownload -bool false
sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate CriticalUpdateInstall -bool false
sudo defaults write /Library/Preferences/com.apple.SoftwareUpdate ConfigDataInstall -bool false
sudo defaults write /Library/Preferences/com.apple.commerce AutoUpdate -bool false
sudo defaults write /Library/Preferences/com.apple.commerce AutoUpdateRestartRequired -bool false

# Disable window restoration on login (stops all apps reopening after restart)
defaults write com.apple.loginwindow TALLogoutSavesState -bool false
defaults write NSGlobalDomain NSQuitAlwaysKeepsWindows -bool false

# Disable Chrome auto-update (Chrome 128 is the ceiling for Catalina anyway)
sudo defaults write com.google.Keystone.Agent checkInterval 0
```

The LaunchAgent daemon is unaffected by these settings — it is managed by `launchctl`,
not by Login Items, and survives restarts automatically via `KeepAlive: true`.
