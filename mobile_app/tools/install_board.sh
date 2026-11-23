#!/usr/bin/env bash
# Run as radxa on the existing V3 board. No model downloads or baseline overwrites.
set -euo pipefail
source_dir="$(cd "$(dirname "$0")/.." && pwd)"
target="$HOME/tu/paperscan/app"
mkdir -p "$target" "$HOME/tu/paperscan/jobs" "$HOME/tu/paperscan/pipeline"
cp -R "$source_dir/backend" "$source_dir/web" "$target/"
cp "$source_dir/pipeline/"*.py "$HOME/tu/paperscan/pipeline/"
cp "$source_dir/pipeline/version.json" "$HOME/tu/paperscan/pipeline/"
service_dir="$HOME/.config/systemd/user"
mkdir -p "$service_dir"
cat > "$service_dir/cix-paperscan.service" <<EOF
[Unit]
Description=CIX PaperScan App Gateway
After=network.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 $target/backend/server.py --runner $HOME/tu/paperscan/pipeline/run_single_page_video.py --results-root $HOME/tu/workspace
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=15
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now cix-paperscan.service
echo '服务：8088。配对码：~/tu/paperscan/jobs/pairing-token.txt'
echo '当前为结果查看模式，无需摄像头或模型服务。板端导出结果每 15 秒自动同步。'
echo '若需要退出 SSH 或重启后自动运行，请由管理员为该用户启用 systemd lingering。'
hostname -I
