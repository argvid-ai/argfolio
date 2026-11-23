#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$script_dir/web_cam_singlepage.py" \
  --cam auto --capture raw --width 3840 --height 2160 --fps 30 \
  --http 8080 --bitrate 30M \
  --out-dir "${PAPERSCAN_RECORDINGS_DIR:-$HOME/tu/recordings}"
