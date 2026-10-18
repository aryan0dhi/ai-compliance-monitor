#!/bin/sh
# Compute Engine startup script (runs as root at every boot): make sure Docker and the compose
# plugin are installed. The containers themselves restart on their own (restart: unless-stopped).
set -eu

if ! command -v docker >/dev/null 2>&1; then
    apt-get update -q
    apt-get install -y -q ca-certificates curl
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh
    systemctl enable --now docker
fi

# A little swap so image builds don't run out of memory on small machine types.
if [ ! -f /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon /swapfile 2>/dev/null || true
