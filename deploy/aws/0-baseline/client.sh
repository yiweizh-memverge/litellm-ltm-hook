#!/usr/bin/env bash
# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0
#
# Phase 0, VM-2: the customer's Open WebUI (Corp SSO, one shared LiteLLM key) and
# an employee's Claude Code, both stock. Run after phase 0 on VM-1, with the
# handoff file it wrote copied here:
#
#   bash deploy/aws/0-baseline/client.sh [path/to/client-baseline.env]
#
# Demo stand-in: socat units (ltm-forward@<port>) forward 127.0.0.1:<port> to VM-1:
# 8180 (Keycloak) here, 8100 (the portal) in phase 1. This host, VM-1 and your
# browser (ssh tunnel to this host) then all use http://127.0.0.1:<port>. In a real
# company those are DNS names; nothing here depends on them being forwards.
# shellcheck source=../lib.sh
. "$(dirname "$0")/../lib.sh"

prepare
IN=${1:-$HANDOFF/client-baseline.env}
[[ -s $IN ]] || die "no handoff file $IN: copy VM-1's $HANDOFF/client-baseline.env here"
# shellcheck disable=SC1090
. "$IN"
: "${SERVER_IP:?}" "${OAUTH_CLIENT_SECRET:?}" "${OPENWEBUI_APP_KEY:?}" "${ALICE_VIRTUAL_KEY:?}" "${ADMIN_PASSWORD:?}"

c_packages() {
  sudo apt-get update -q
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq git curl python3 socat
  install_uv
}

c_forward() {
  sudo tee /etc/systemd/system/ltm-forward@.service >/dev/null <<EOF
[Unit]
Description=demo stand-in for a server DNS name: 127.0.0.1:%i -> VM-1:%i
After=network-online.target

[Service]
ExecStart=/usr/bin/socat TCP-LISTEN:%i,bind=127.0.0.1,fork,reuseaddr TCP:$SERVER_IP:%i
Restart=always

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now ltm-forward@8180
  sudo systemctl restart ltm-forward@8180   # picks up a changed SERVER_IP on reruns
  wait_for http://127.0.0.1:8180/realms/corp/.well-known/openid-configuration 60
}

c_install() {
  cd "$ENV_DIR"
  mkdir -p openwebui data/openwebui
  [[ -x openwebui/.venv/bin/open-webui ]] || {
    uv venv openwebui/.venv --python 3.12
    VIRTUAL_ENV=openwebui/.venv uv pip install "open-webui==$OPENWEBUI_VERSION"; }
}

c_env() {
  # The customer's Open WebUI: Corp SSO, one connection to LiteLLM with a shared key.
  # Nothing about memory yet.
  (umask 077
   [[ -s $STATE/webui-secret ]] || rand 32 >"$STATE/webui-secret"
   cat >"$ENV_DIR/openwebui.env" <<EOF
OPENWEBUI_PORT=8087
DATA_DIR=$ENV_DIR/data/openwebui
DATABASE_URL=sqlite:///$ENV_DIR/data/openwebui/webui.db
WEBUI_SECRET_KEY=$(cat "$STATE/webui-secret")

ENABLE_OAUTH_SIGNUP=true
OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true
OAUTH_CLIENT_ID=open-webui
OAUTH_CLIENT_SECRET=$OAUTH_CLIENT_SECRET
OPENID_PROVIDER_URL=http://127.0.0.1:8180/realms/corp/.well-known/openid-configuration
OAUTH_PROVIDER_NAME="Corp SSO"
OAUTH_SCOPES="openid email profile"
ENABLE_LOGIN_FORM=false
DEFAULT_USER_ROLE=user
TASK_MODEL_EXTERNAL=glm-5.2
BYPASS_MODEL_ACCESS_CONTROL=true

ENABLE_OLLAMA_API=false
ENABLE_MEMORIES=false
# Nothing that remembers, or speaks for the user, besides MemMachine: no notes,
# no clickable follow-up suggestions (one click sends a sentence the model made
# up), and no built-in tools (notes, chat search, ...) for any model.
ENABLE_NOTES=false
ENABLE_FOLLOW_UP_GENERATION=false
DEFAULT_MODEL_METADATA='{"capabilities":{"builtin_tools":false}}'
ANONYMIZED_TELEMETRY=false
DO_NOT_TRACK=true
SCARF_NO_ANALYTICS=true
EOF
   cat >"$ENV_DIR/openwebui-connection.env" <<EOF
OPENAI_API_BASE_URLS=http://$SERVER_IP:4011/v1
OPENAI_API_KEYS=$OPENWEBUI_APP_KEY
EOF
  )
}

c_openwebui() {
  sudo cp "$REPO/deploy/systemd/ltmplat-openwebui.service" /etc/systemd/system/
  # On 0.0.0.0 for VM-1's end-to-end checks (verify.sh); the SG lets only VM-1 in.
  # Your browser comes through the ssh tunnel to this host.
  override_execstart ltmplat-openwebui "$ENV_DIR/openwebui/.venv/bin/open-webui serve" \
    "--host 0.0.0.0 --port \${OPENWEBUI_PORT}"
  sudo systemctl daemon-reload
  sudo systemctl enable --now ltmplat-openwebui
  wait_for http://127.0.0.1:8087/api/config 600   # first start downloads its assets
  owui_admin_first http://127.0.0.1:8087 "$ADMIN_PASSWORD"
}

c_claude_code() {
  command -v claude >/dev/null || curl -fsSL https://claude.ai/install.sh | bash
  mkdir -p ~/.claude
  if [[ -s ~/.claude/settings.json ]]; then
    echo "~/.claude/settings.json exists; left alone"
    return
  fi
  # Alice's own key against the gateway; the old hook gives memmachine- models memory.
  (umask 077; python3 - "$SERVER_IP" "$ALICE_VIRTUAL_KEY" >~/.claude/settings.json <<'PY'
import json, sys
ip, key = sys.argv[1:]
print(json.dumps({"env": {
    "ANTHROPIC_BASE_URL": f"http://{ip}:4011",
    "ANTHROPIC_AUTH_TOKEN": key,
    "ANTHROPIC_MODEL": "memmachine-glm-5.2",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.2",
}}, indent=2))
PY
  )
  echo "Claude Code: ~/.claude/settings.json written for Alice"
}

step c00-packages    c_packages
step c01-forward     c_forward
step c02-install     c_install
step c03-env         c_env
step c04-openwebui   c_openwebui
step c05-claude-code c_claude_code

cat <<EOF

==============================================================================
 Phase 0, VM-2 is up: Open WebUI (stock, Corp SSO) and Claude Code (Alice).
 Delete the handoff file now: rm $IN
 From your laptop (README section 4), one tunnel to this host:

   ssh -N -L 8087:127.0.0.1:8087 -L 8180:127.0.0.1:8180 -L 8100:127.0.0.1:8100 \\
       ubuntu@<VM-2 public IP>
==============================================================================
EOF
