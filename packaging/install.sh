#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

PRESERVE_CONFIG=1
CONFIG_SOURCE="examples/config.example.json"
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

for argument in "$@"; do
  case "$argument" in
    --overwrite-config) PRESERVE_CONFIG=0 ;;
    --config=*) CONFIG_SOURCE="${argument#--config=}" ;;
    *) echo "Unknown argument: $argument" >&2; exit 2 ;;
  esac
done

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

CONFIG_SOURCE=$(realpath "$CONFIG_SOURCE")
[[ -f "$CONFIG_SOURCE" ]] || { echo "Config source not found." >&2; exit 1; }

WAS_ACTIVE=0
if systemctl is-active --quiet modbus-edge-agent 2>/dev/null; then
  WAS_ACTIVE=1
fi

useradd --system --home /var/lib/modbus-edge-agent --shell /usr/sbin/nologin modbus-edge 2>/dev/null || true
usermod --append --groups dialout modbus-edge
install -d -o root -g root -m 0755 /opt/modbus-edge-agent /etc/modbus-edge-agent
install -d -o modbus-edge -g modbus-edge -m 0750 /var/lib/modbus-edge-agent

if ! dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -q 'ok installed'; then
  apt-get update
  env DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv
fi

python3 -m venv /opt/modbus-edge-agent/venv
/opt/modbus-edge-agent/venv/bin/pip install --disable-pip-version-check --upgrade "$ROOT_DIR"
install -o root -g root -m 0644 "$ROOT_DIR/packaging/modbus-edge-agent.service" /etc/systemd/system/modbus-edge-agent.service

if [[ ! -f /etc/modbus-edge-agent/config.json || "$PRESERVE_CONFIG" -eq 0 ]]; then
  install -o root -g modbus-edge -m 0640 "$CONFIG_SOURCE" /etc/modbus-edge-agent/config.json
else
  chown root:modbus-edge /etc/modbus-edge-agent/config.json
  chmod 0640 /etc/modbus-edge-agent/config.json
fi

runuser -u modbus-edge --   /opt/modbus-edge-agent/venv/bin/modbus-edge-agent   --config /etc/modbus-edge-agent/config.json   --validate-config

systemctl daemon-reload
if [[ "$WAS_ACTIVE" -eq 1 ]]; then
  systemctl restart modbus-edge-agent
  echo "Restarted the previously active service."
fi

echo "Installed but not enabled automatically."
echo "Dry-run: runuser -u modbus-edge -- /opt/modbus-edge-agent/venv/bin/modbus-edge-agent --config /etc/modbus-edge-agent/config.json --dry-run --no-mqtt --once"
echo "Enable after bench acceptance: systemctl enable modbus-edge-agent, then systemctl start modbus-edge-agent"
