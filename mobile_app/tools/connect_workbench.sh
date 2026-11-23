#!/usr/bin/env bash
# Foreground SSH tunnel. Also works with passwordless SSH authentication.
set -euo pipefail
config="$(cd "$(dirname "$0")" && pwd)/board-ssh.env"
if [[ ! -f "$config" ]]; then
  echo "Create $config from board-ssh.env.example first." >&2
  exit 2
fi
set -a
# This is a user-owned local file, ignored by Git.
source "$config"
set +a
: "${PAPERSCAN_SSH_USER:?Set PAPERSCAN_SSH_USER}"
: "${PAPERSCAN_SSH_HOST:?Set PAPERSCAN_SSH_HOST}"
: "${PAPERSCAN_SSH_PORT:?Set PAPERSCAN_SSH_PORT}"
if [[ ! "$PAPERSCAN_SSH_PORT" =~ ^[0-9]+$ ]]; then
  echo "PAPERSCAN_SSH_PORT must be numeric." >&2
  exit 2
fi
mode="${1:-workbench}"
case "$mode" in
  workbench) forward='127.0.0.1:8088:127.0.0.1:8089' ;;
  phone)
    : "${PAPERSCAN_PHONE_BIND_IP:?Set PAPERSCAN_PHONE_BIND_IP for phone mode}"
    forward="${PAPERSCAN_PHONE_BIND_IP}:8088:127.0.0.1:8088"
    ;;
  *) echo 'Usage: connect_workbench.sh [workbench|phone]' >&2; exit 2 ;;
esac
exec ssh -N -p "$PAPERSCAN_SSH_PORT" -o ExitOnForwardFailure=yes \
  -L "$forward" "${PAPERSCAN_SSH_USER}@${PAPERSCAN_SSH_HOST}"
