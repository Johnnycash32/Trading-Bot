#!/usr/bin/env bash
set -euo pipefail

echo "=== ORBITIQ Safe Update ==="

cd /home/ubuntu/Trading-Bot

echo "[1/5] Backing up paper state..."
cp .paper-state.json .paper-state.json.bak 2>/dev/null || true
cp .paper-copy-settings.json .paper-copy-settings.json.bak 2>/dev/null || true

echo "[2/5] Fetching and merging (no reset --hard)..."
git fetch origin
git merge origin/main --no-edit

echo "[3/5] Updating Python dependencies..."
.venv/bin/pip install -r requirements.txt --quiet

echo "[4/5] Restarting service..."
sudo systemctl restart orbitiq

echo "[5/5] Waiting for service to be ready..."
sleep 3
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/ | grep -q "200"; then
    echo "✅ ORBITIQ is running at http://$(hostname -I | awk '{print $1}'):8765"
else
    echo "⚠️  Service may still be starting. Check: sudo journalctl -u orbitiq -n 20"
fi