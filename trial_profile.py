"""
Quick trial run: profile the 20 artists in spotify_genres_sample.json.
Song counts are equal (1 each) since the sample has no counts.

Usage:
    LASTFM_API_KEY=xxx python trial_profile.py
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
from lastfm import profile_artists

API_KEY = os.environ.get("LASTFM_API_KEY", "")
if not API_KEY:
    sys.exit("Set LASTFM_API_KEY env var first")

with open("spotify_genres_sample.json") as f:
    raw = json.load(f)

artists = [{"name": name, "song_count": 1} for name in raw]

async def main():
    print(f"Profiling {len(artists)} artists...\n")
    result = await profile_artists(artists, API_KEY)

    print("=== PIE CHART (top 10 genres) ===")
    for genre, share in result["genres"].items():
        bar = "█" * int(share * 40)
        print(f"  {genre:<30} {share*100:5.1f}%  {bar}")

    print(f"\n=== BAND BUBBLE COLORS ===")
    for a in result["artists"]:
        print(f"  {a['name']:<25} -> {a['band_color']}")

    print(f"\n  {result['tagged_count']}/{result['artist_count']} artists recognised by Last.fm")

asyncio.run(main())
