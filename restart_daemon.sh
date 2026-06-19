#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "==> Pulling latest from GitHub..."
git pull

echo "==> Restarting profiler daemon..."
launchctl unload ~/Library/LaunchAgents/com.jukebar.profiler.plist 2>/dev/null || true
launchctl load  ~/Library/LaunchAgents/com.jukebar.profiler.plist

echo "==> Done. Log tail:"
sleep 2
tail -10 ~/jukebarweb/daemon.log
