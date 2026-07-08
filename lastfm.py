"""
/Users/laszlo/PycharmProjects/jukebarweb/lastfm.py

RESPONSIBILITY: The only module that calls the Last.fm API. Fetches raw
  artist tags and resolves them into a palette genre + weighted pie
  contribution, using tag_map.json (repo root) as the tag-to-genre lookup.
CALLED BY: profile_daemon.py, on MacLord, once per unprofiled artist per
  cycle. main.py never calls this directly — it only ever reads the
  already-profiled results back from GCS.
KEY METHODS:
  - fetch_artist_tags(client, artist, api_key) — raw Last.fm top tags
  - resolve_artist(name, song_count, tags, top_n) — band_color + pie_scores;
    this is where a raw tag list becomes one palette genre
  - compute_playlist_pie(all_pie_scores) — normalised top-10 pie
  - combine_playlist_pies(pies_with_weights) — merge multiple playlists
  - profile_artists(artists, api_key) — legacy, kept only for trial_profile.py

Edit tag_map.json, push, and Render/daemon picks it up on restart.
"""
from __future__ import annotations
import asyncio
import json
import os
import httpx

LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
_OTHER = {"pie": "other", "band": "other"}

_tag_map_path = os.path.join(os.path.dirname(__file__), "tag_map.json")
with open(_tag_map_path) as _f:
    TAG_MAP: dict[str, dict] = json.load(_f)


async def fetch_artist_tags(
    client: httpx.AsyncClient,
    artist: str,
    api_key: str,
) -> list[tuple[str, int]]:
    """Fetch top tags for an artist from Last.fm. Returns [(tag, count), ...]."""
    try:
        r = await client.get(LASTFM_API, params={
            "method":      "artist.getTopTags",
            "artist":      artist,
            "api_key":     api_key,
            "format":      "json",
            "autocorrect": 1,
        }, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        if "error" in data:
            return []
        tags = data.get("toptags", {}).get("tag", [])
        return [(t["name"], int(t["count"])) for t in tags if isinstance(t, dict) and t.get("count")]
    except Exception:
        return []


def resolve_artist(
    name: str,
    song_count: int,
    tags: list[tuple[str, int]],
    top_n: int = 4,
) -> dict:
    """
    Given raw Last.fm tags, compute band_color and weighted pie contributions.

    Returns:
        {
          "band_color":    str,             # e.g. "indie", "heavy metal", "other"
          "pie_category":  str,             # top non-other pie category (or "other")
          "pie_scores":    {str: float},    # {pie_category: weighted_score}
        }
    """
    pie_scores: dict[str, float] = {}
    band_color = "other"
    pie_category = "other"

    for tag_name, tag_count in tags[:top_n]:
        mapping = TAG_MAP.get(tag_name.lower().strip(), _OTHER)
        score = song_count * tag_count
        cat = mapping["pie"]
        pie_scores[cat] = pie_scores.get(cat, 0.0) + score
        if band_color == "other" and mapping["band"] != "other":
            band_color = mapping["band"]
            pie_category = cat

    return {"band_color": band_color, "pie_category": pie_category, "pie_scores": pie_scores}


def compute_playlist_pie(
    all_pie_scores: list[dict[str, float]],
    top_n: int = 10,
) -> dict[str, float]:
    """Combine per-artist pie_scores into a normalised top-N pie chart."""
    totals: dict[str, float] = {}
    for scores in all_pie_scores:
        for cat, val in scores.items():
            totals[cat] = totals.get(cat, 0.0) + val
    total = sum(totals.values()) or 1.0
    top = sorted(totals.items(), key=lambda x: -x[1])[:top_n]
    return {k: round(v / total, 4) for k, v in top}


def combine_playlist_pies(
    pies_with_weights: list[tuple[dict[str, float], int]],
    top_n: int = 10,
) -> dict[str, float]:
    """Combine per-playlist pies weighted by artist count. Returns top-N combined pie."""
    totals: dict[str, float] = {}
    total_weight = 0
    for pie, weight in pies_with_weights:
        for cat, frac in pie.items():
            totals[cat] = totals.get(cat, 0.0) + frac * weight
        total_weight += weight
    if not total_weight:
        return {}
    total = sum(totals.values()) or 1.0
    top = sorted(totals.items(), key=lambda x: -x[1])[:top_n]
    return {k: round(v / total, 4) for k, v in top}


async def profile_artists(
    artists: list[dict],
    api_key: str,
    *,
    top_n_tags: int = 4,
    rate_delay: float = 0.25,
) -> dict:
    """
    Legacy function — profile a flat list of {name, song_count} artists.
    Used by trial_profile.py.
    """
    pie_totals: dict[str, float] = {}
    artist_results = []
    tagged = 0

    async with httpx.AsyncClient() as client:
        for entry in artists:
            name = entry["name"]
            song_count = entry.get("song_count", 1)

            raw_tags = await fetch_artist_tags(client, name, api_key)
            await asyncio.sleep(rate_delay)

            if not raw_tags:
                artist_results.append({"name": name, "band_color": "other"})
                continue

            tagged += 1
            resolved = resolve_artist(name, song_count, raw_tags, top_n_tags)
            for cat, score in resolved["pie_scores"].items():
                pie_totals[cat] = pie_totals.get(cat, 0.0) + score
            artist_results.append({"name": name, "band_color": resolved["band_color"]})

    total = sum(pie_totals.values()) or 1.0
    top_genres = sorted(pie_totals.items(), key=lambda x: -x[1])[:10]
    genres = {g: round(s / total, 4) for g, s in top_genres}

    return {
        "genres":       genres,
        "artists":      artist_results,
        "artist_count": len(artists),
        "tagged_count": tagged,
    }
