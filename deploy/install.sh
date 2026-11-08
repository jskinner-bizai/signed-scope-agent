#!/usr/bin/env bash
# Installs Signed Scope Agent on a fresh Ubuntu 24.04 Vultr Cloud Compute instance.
# Run as root. Optional env: SSA_REPO, SSA_SETUP_TOKEN, SSA_HOST.
set -euxo pipefail

SSA_REPO="${SSA_REPO:-https://github.com/jskinner-bizai/signed-scope-agent.git}"
DIR=/opt/ssa

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y docker.io git python3-venv python3-pip caddy curl
systemctl enable --now docker

[ -d "$DIR/.git" ] || git clone "$SSA_REPO" "$DIR"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
docker pull python:3.12-slim

mkdir -p "$DIR/data"
if [ -n "${SSA_SETUP_TOKEN:-}" ]; then
  echo "$SSA_SETUP_TOKEN" > "$DIR/data/setup.token"
  chmod 600 "$DIR/data/setup.token"
fi

# Public hostname with automatic HTTPS: <ip-with-dashes>.sslip.io
IP="$(curl -fsS http://169.254.169.254/v1/interfaces/0/ipv4/address || hostname -I | awk '{print $1}')"
SSA_HOST="${SSA_HOST:-$(echo "$IP" | tr . -).sslip.io}"

cat > /etc/ssa.env <<ENV
SSA_DATA=$DIR/data
SANDBOX_IMAGE=python:3.12-slim
DEMO_UPSTREAM=http://host.docker.internal:8000
DEMO_PACE=6
ENV

cat > /etc/systemd/system/ssa.service <<UNIT
[Unit]
Description=Signed Scope Agent
After=docker.service network-online.target
Requires=docker.service

[Service]
EnvironmentFile=/etc/ssa.env
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/caddy/Caddyfile <<CADDY
$SSA_HOST {
  reverse_proxy 127.0.0.1:8000
}
CADDY

# Firewall: web from anywhere, port 8000 only from the sandbox proxy on the docker bridge.
if command -v ufw >/dev/null; then
  ufw allow 80/tcp; ufw allow 443/tcp
  ufw allow in on docker0 to any port 8000 proto tcp
fi

# Pull new commits from the repo every 5 minutes and restart when they change.
cat > /usr/local/bin/ssa-update <<'UPD'
#!/usr/bin/env bash
cd /opt/ssa || exit 0
old=$(git rev-parse HEAD); git pull -q --ff-only || exit 0
if [ "$old" != "$(git rev-parse HEAD)" ]; then
  /opt/ssa/.venv/bin/pip install -q -r requirements.txt
  systemctl restart ssa
fi
UPD
chmod +x /usr/local/bin/ssa-update
echo "*/5 * * * * root /usr/local/bin/ssa-update" > /etc/cron.d/ssa-update

systemctl daemon-reload
systemctl enable --now ssa
systemctl restart caddy
echo "Signed Scope Agent is at https://$SSA_HOST"
