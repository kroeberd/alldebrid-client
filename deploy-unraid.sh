#!/bin/bash
# AllDebrid-Client — Unraid Deployment Script
# Run this on your Unraid server via SSH

set -e

APPDATA="/mnt/user/appdata/alldebrid-client"
IMAGE="alldebrid-client:latest"
CONTAINER="alldebrid-client"

echo "=== AllDebrid-Client Deploy ==="
echo ""

# 1. Stop & remove old container (but keep volumes/config)
echo "[1/5] Stopping container..."
docker stop "$CONTAINER" 2>/dev/null && echo "  Stopped" || echo "  (not running)"
docker rm "$CONTAINER" 2>/dev/null && echo "  Removed" || echo "  (not found)"

# 2. Remove old image to force clean build
echo "[2/5] Removing old image..."
docker rmi "$IMAGE" 2>/dev/null && echo "  Removed" || echo "  (not found)"

# 3. Build fresh
echo "[3/5] Building new image (no cache)..."
cd "$APPDATA"
docker build --no-cache -t "$IMAGE" .
echo "  Build OK"

# 4. Recreate folders
echo "[4/5] Ensuring folders exist..."
mkdir -p \
  "$APPDATA/config" \
  /mnt/user/Downloads/Test/torrents/watch \
  /mnt/user/Downloads/Test/torrents/processed \
  /mnt/user/Downloads/Test/torrents/downloads

# 5. Start container
echo "[5/5] Starting container..."
docker run -d \
  --name="$CONTAINER" \
  --net=bridge \
  --pids-limit=2048 \
  --restart=unless-stopped \
  -e TZ="Europe/Berlin" \
  -e CONFIG_PATH="/app/config/config.json" \
  -e DB_PATH="/app/config/alldebrid.db" \
  -l net.unraid.docker.managed=dockerman \
  -l "net.unraid.docker.webui=http://[IP]:[PORT:8080]/" \
  -p "9999:8080/tcp" \
  -v "$APPDATA/config:/app/config:rw" \
  -v "/mnt/user/Downloads/Test/torrents/watch:/app/data/watch:rw" \
  -v "/mnt/user/Downloads/Test/torrents/processed:/app/data/processed:rw" \
  -v "/mnt/user/Downloads/Test/torrents/downloads:/app/data/downloads:rw" \
  "$IMAGE"

echo ""
echo "=== Deploy complete ==="
echo "Logs: docker logs -f $CONTAINER"
echo "UI:   http://$(hostname -I | awk '{print $1}'):9999"
echo ""
echo "Waiting for startup..."
sleep 5
docker logs --tail=10 "$CONTAINER"
