#!/usr/bin/env bash
# Runs INSIDE the agent LXC as root. Prepares it to receive deploys from the runner.
# Env: DEPLOY_PUBKEY (runner's deploy key), INSTALLER (path to pushed sensi-agent-install)
#
# The `deploy` user can only stage files and run the root-owned installer via sudo;
# the installer writes the systemd unit itself, so a deploy can't change what runs as root.
set -euo pipefail
: "${DEPLOY_PUBKEY:?}" "${INSTALLER:?}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv rsync sudo openssh-server ca-certificates >/dev/null
systemctl enable --now ssh >/dev/null

id sensi-agent >/dev/null 2>&1 || useradd --system --home-dir /var/lib/sensi-agent --shell /usr/sbin/nologin sensi-agent
id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy

install -d -m 700 -o deploy -g deploy /home/deploy/.ssh /home/deploy/staging
echo "$DEPLOY_PUBKEY" > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

install -d -m 755 /opt/sensi-agent
install -d -m 750 -g sensi-agent /etc/sensi-agent
install -m 750 -o root -g root "$INSTALLER" /usr/local/sbin/sensi-agent-install

echo 'deploy ALL=(root) NOPASSWD: /usr/local/sbin/sensi-agent-install' > /etc/sudoers.d/sensi-agent-deploy
chmod 440 /etc/sudoers.d/sensi-agent-deploy
visudo -cf /etc/sudoers.d/sensi-agent-deploy >/dev/null

echo "agent LXC ready"
