"""
~/jukebarweb/profile_daemon.py on MacLord (same repo as jukebarweb, cloned locally)

RESPONSIBILITY: The genre-profiling loop. Runs on MacLord — a physical
  always-on Mac at the user's home, NOT Render — as a LaunchAgent
  (com.jukebar.profiler.plist), not as part of the web dyno. Reads/writes
  GCS directly; makes no calls to jukebarweb's own API at all.
CALLED BY: Nobody — it's the entry point, started by launchd on boot and
  kept alive lid-closed via `caffeinate -is` in the plist. Restarted by
  restart_daemon.sh (git pull + launchctl reload).
KEY METHODS / flow (top to bottom of main loop):
  1. Load band_cache.json from GCS (global artist -> {pie, band, tags} cache)
  2. Load map_entries.json from GCS (bar list + playlists with updated_at,
     written by main.py's map_register() whenever a host registers)
  3. For each bar, read map/{bar_id}/profile.json from GCS
  4. For each playlist: if updated_at > profiled_at, re-profile via Last.fm
     (lastfm.py) — check band_cache first, only call Last.fm for unknowns
  5. Recombine all profiled playlists' pies into combined_pie
  6. Write updated map/{bar_id}/profile.json + band_cache.json back to GCS —
     main.py picks this up later on its own polling timer, never pushed

Usage:
    python3 profile_daemon.py [--once]

Env vars:
    GCS_BUCKET                       — GCS bucket name (required)
    GOOGLE_APPLICATION_CREDENTIALS   — path to service account JSON key file
    LASTFM_API_KEY                   — Last.fm API key
    PROFILE_POLL_INTERVAL            — seconds between polls (default 300)
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
import time

import httpx

import gcs_store
from lastfm import (
    TAG_MAP, fetch_artist_tags, resolve_artist,
    compute_playlist_pie, combine_playlist_pies,
)

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")
POLL_INTERVAL  = int(os.environ.get("PROFILE_POLL_INTERVAL", "300"))
TOP_N_TAGS     = 4
RATE_DELAY     = 0.25   # seconds between Last.fm requests (~4 req/s)


# ── GCS helpers ───────────────────────────────────────────────────────────────

def _read_json(path: str, default: dict) -> dict:
    raw = gcs_store.read(path)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _write_json(path: str, data: dict) -> None:
    gcs_store.write(path, json.dumps(data, indent=2))


def load_map_entries() -> dict:
    return _read_json("map_entries.json", {})


def load_band_cache() -> dict:
    return _read_json("band_cache.json", {})


def save_band_cache(cache: dict) -> None:
    _write_json("band_cache.json", cache)
    print(f"[daemon] band_cache saved ({len(cache)} artists)", flush=True)


def load_bar_profile(bar_id: str) -> dict:
    return _read_json(f"map/{bar_id}/profile.json", {
        "playlist_profiled_at": {},
        "playlists": {},
        "combined_pie": {},
    })


def save_bar_profile(bar_id: str, profile: dict) -> None:
    _write_json(f"map/{bar_id}/profile.json", profile)


# ── Profiling ─────────────────────────────────────────────────────────────────

async def _profile_playlist(
    artist_entries: list,
    band_cache: dict,
    api_key: str,
) -> tuple[dict, list[dict]]:
    """
    Profile one playlist's artists.
    artist_entries: list of str or {name, song_count} dicts.
    Checks band_cache first; only calls Last.fm for unknown artists.
    Returns (pie_chart, artists_list).
    """
    all_pie_scores: list[dict] = []
    artists_out: list[dict] = []

    async with httpx.AsyncClient() as client:
        for entry in artist_entries:
            if isinstance(entry, dict):
                name       = entry.get("name", "")
                song_count = entry.get("song_count", 1)
            else:
                name       = str(entry)
                song_count = 1
            if not name:
                continue

            if name not in band_cache:
                tags = await fetch_artist_tags(client, name, api_key)
                await asyncio.sleep(RATE_DELAY)
                if tags:
                    resolved = resolve_artist(name, 1, tags, TOP_N_TAGS)
                    band_cache[name] = {
                        "pie":  resolved["pie_category"],
                        "band": resolved["band_color"],
                        "tags": [list(t) for t in tags[:TOP_N_TAGS]],
                    }
                    print(f"    {name} → {resolved['band_color']}", flush=True)
                else:
                    band_cache[name] = {"pie": "other", "band": "other", "tags": []}
                    print(f"    {name} → (no Last.fm data)", flush=True)

            cached = band_cache[name]
            tags_for_resolve = [tuple(t) for t in cached.get("tags", [])]
            resolved = resolve_artist(name, song_count, tags_for_resolve, TOP_N_TAGS)
            all_pie_scores.append(resolved["pie_scores"])
            artists_out.append({
                "name":       name,
                "song_count": song_count,
                "band_color": resolved["band_color"],
                "tags":       [list(t) for t in tags_for_resolve[:TOP_N_TAGS]],
            })

    pie = compute_playlist_pie(all_pie_scores)

    # Log top-5 artists by song count so weighting effect is visible
    total_songs = sum(a["song_count"] for a in artists_out)
    top5 = sorted(artists_out, key=lambda a: -a["song_count"])[:5]
    top5_str = ", ".join(a["name"] + " x" + str(a["song_count"]) + " (" + a["band_color"] + ")" for a in top5)
    print(f"    song counts — total tracks: {total_songs}, top artists: {top5_str}", flush=True)

    # Compare weighted vs flat (every artist = 1) to show weighting impact
    flat_scores = [resolve_artist(a["name"], 1,
                                  [tuple(t) for t in band_cache.get(a["name"], {}).get("tags", [])],
                                  TOP_N_TAGS)["pie_scores"]
                   for a in artists_out]
    flat_pie = compute_playlist_pie(flat_scores)
    weighted_top = list(pie.items())[:3]
    flat_top     = list(flat_pie.items())[:3]
    print(f"    weighted pie: {weighted_top}  |  flat (×1): {flat_top}", flush=True)

    return pie, artists_out


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run_once(band_cache: dict) -> int:
    """Check all bars, profile stale playlists. Returns number of bars updated."""
    if not LASTFM_API_KEY:
        print("[daemon] LASTFM_API_KEY not set — aborting", flush=True)
        return 0

    map_entries = load_map_entries()
    if not map_entries:
        print("[daemon] no bars in map_entries.json", flush=True)
        return 0

    updated_bars = 0

    for bar_id, entry in map_entries.items():
        bar_name = entry.get("bar_name", bar_id)
        playlists = entry.get("playlists", [])
        if not playlists:
            continue

        profile = load_bar_profile(bar_id)
        playlist_profiled_at: dict = profile.get("playlist_profiled_at", {})
        profile_playlists: dict = profile.get("playlists", {})
        bar_changed = False

        for pl in playlists:
            pl_name     = pl.get("name", "Playlist")
            updated_at  = pl.get("updated_at", 0)
            profiled_at = playlist_profiled_at.get(pl_name, 0)

            if updated_at <= profiled_at:
                continue  # up to date

            artist_entries = pl.get("artists", [])
            if not artist_entries:
                continue

            print(f"[daemon] {bar_name} / {pl_name}: {len(artist_entries)} artists", flush=True)
            t0 = time.time()
            pie, artists = await _profile_playlist(artist_entries, band_cache, LASTFM_API_KEY)
            elapsed = time.time() - t0
            top4 = list(pie.items())[:4]
            print(f"[daemon] {bar_name} / {pl_name}: done in {elapsed:.1f}s — {top4}", flush=True)

            profile_playlists[pl_name] = {"pie": pie, "artists": artists}
            playlist_profiled_at[pl_name] = time.time()
            bar_changed = True

        if bar_changed:
            pies_with_weights = [
                (pd["pie"], len(pd["artists"]))
                for pd in profile_playlists.values()
                if pd.get("pie")
            ]
            combined = combine_playlist_pies(pies_with_weights)

            profile["playlist_profiled_at"] = playlist_profiled_at
            profile["playlists"]            = profile_playlists
            profile["combined_pie"]         = combined

            save_bar_profile(bar_id, profile)
            top4 = list(combined.items())[:4]
            print(f"[daemon] {bar_name}: combined pie saved — {top4}", flush=True)
            updated_bars += 1

    return updated_bars


async def main() -> None:
    if not os.environ.get("GCS_BUCKET"):
        print("[daemon] GCS_BUCKET not set — aborting", flush=True)
        sys.exit(1)

    once = "--once" in sys.argv

    print(f"[daemon] loading band_cache from GCS...", flush=True)
    band_cache = load_band_cache()
    print(f"[daemon] band_cache loaded ({len(band_cache)} artists)", flush=True)

    if once:
        updated = await run_once(band_cache)
        save_band_cache(band_cache)
        print(f"[daemon] done — {updated} bar(s) updated", flush=True)
        return

    print(f"[daemon] starting — polling every {POLL_INTERVAL}s", flush=True)
    while True:
        try:
            updated = await run_once(band_cache)
            save_band_cache(band_cache)
            print(f"[daemon] cycle done — {updated} bar(s) updated", flush=True)
        except Exception as e:
            import traceback
            print(f"[daemon] unexpected error: {e}", flush=True)
            traceback.print_exc()
        print(f"[daemon] sleeping {POLL_INTERVAL}s...", flush=True)
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
