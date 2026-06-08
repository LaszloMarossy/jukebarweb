"""
Spotify genre exploration script.
Queries artist genres for a sample band list and fetches the full
available-genre-seeds list to help build our micro→macro genre mapping.

Usage:
    SPOTIFY_CLIENT_ID=xxx SPOTIFY_CLIENT_SECRET=yyy python explore_spotify_genres.py
"""
import os, json, collections
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

SAMPLE_BANDS = [
    "Radiohead", "Metallica", "The Clash", "Daft Punk", "Miles Davis",
    "Kendrick Lamar", "Johnny Cash", "Beethoven", "Portishead", "Arcade Fire",
    "Slayer", "The Prodigy", "Nick Cave", "Sigur Rós", "Boards of Canada",
    "Chet Baker", "Massive Attack", "PJ Harvey", "Aphex Twin", "Tom Waits",
]

def main():
    client_id     = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET env vars.")
        return

    sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
        client_id=client_id, client_secret=client_secret
    ))

    # 1. Per-artist genre tags for our sample bands
    print("\n" + "=" * 60)
    print("ARTIST GENRE TAGS (sample bands)")
    print("=" * 60)
    all_genres: list[str] = []
    out = {}
    for band in SAMPLE_BANDS:
        results = sp.search(q=f"artist:{band}", type="artist", limit=1)
        items = results["artists"]["items"]
        if not items:
            print(f"  {band:30s}  NOT FOUND")
            continue
        # Fetch full artist object — search returns simplified (no genres)
        full = sp.artist(items[0]["id"])
        genres = full.get("genres", [])
        all_genres.extend(genres)
        out[band] = genres
        print(f"  {band:30s}  {genres}")

    # 3. Most common micro-genres across the sample
    print("\n" + "=" * 60)
    print("MOST COMMON MICRO-GENRES IN SAMPLE")
    print("=" * 60)
    for genre, count in collections.Counter(all_genres).most_common(50):
        print(f"  {count:3d}  {genre}")

    # 4. Dump full results to JSON for offline analysis
    out_path = "/Users/laszlo/PycharmProjects/jukebarweb/spotify_genres_sample.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nFull results saved to {out_path}")

if __name__ == "__main__":
    main()
