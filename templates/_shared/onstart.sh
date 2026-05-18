#!/bin/bash
# ── SSH client config for Vast.ai proxy ─────────────────────────────────
# The container's entrypoint connects OUT to sshN.vast.ai to set up a
# reverse tunnel (ssh -R PORT:localhost:22 ...).  Without these settings
# the SSH client rejects the unknown host key interactively and exits
# immediately when GatewayPorts is denied, killing the whole tunnel.
mkdir -p /root/.ssh
cat >> /root/.ssh/config << 'EOF'

Host *.vast.ai
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ExitOnForwardFailure no
    ServerAliveInterval 30
    ServerAliveCountMax 5
EOF
chmod 600 /root/.ssh/config
# ────────────────────────────────────────────────────────────────────────

bash /container/entrypoint.sh &
sleep infinity
