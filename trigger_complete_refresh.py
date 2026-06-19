#!/usr/bin/env python3
"""
Reset all playlist_profiled_at timestamps in GCS so the daemon re-profiles
every bar on its next cycle, then SSH to MacLord to restart the daemon.

Usage:
    python3 trigger_complete_refresh.py [maclord-host]

Default MacLord host: 192.168.0.108
"""
import json
import os
import subprocess
import sys

MACLORD = sys.argv[1] if len(sys.argv) > 1 else "192.168.0.108"

os.environ.setdefault("GCS_BUCKET", "jukebar-data")
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS",
                      os.path.join(os.path.dirname(__file__), "gcs-key.json"))

import gcs_store

def reset_timestamps():
    entries = json.loads(gcs_store.read("map_entries.json") or "{}")
    if not entries:
        print("No bars found in map_entries.json")
        return 0
    count = 0
    for bar_id in entries:
        path = f"map/{bar_id}/profile.json"
        raw = gcs_store.read(path)
        if not raw:
            print(f"  {bar_id}: no profile yet, skipping")
            continue
        profile = json.loads(raw)
        profile["playlist_profiled_at"] = {}
        gcs_store.write(path, json.dumps(profile, indent=2))
        print(f"  {bar_id}: timestamps reset")
        count += 1
    return count

def restart_maclord():
    print(f"\n==> Restarting daemon on MacLord ({MACLORD})...")
    result = subprocess.run(
        ["ssh", f"laszlo@{MACLORD}", "~/jukebarweb/restart_daemon.sh"],
        text=True, capture_output=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"SSH error: {result.stderr}")
    return result.returncode == 0

print("==> Resetting playlist timestamps in GCS...")
n = reset_timestamps()
print(f"==> {n} bar(s) reset\n")

if n > 0:
    restart_maclord()
else:
    print("Nothing to do.")
