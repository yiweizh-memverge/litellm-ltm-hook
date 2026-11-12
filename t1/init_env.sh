#!/usr/bin/env bash
# Create the T1 runtime directory ($T1_ENV, default ~/t1-tokenpath-env) with fresh
# secrets. Idempotent: files that already exist are kept.
#
#   t1/init_env.sh
#
# Writes: compose.env, litellm.env, accounts.txt, openwebui.env (all mode 600).
# openwebui.env holds two placeholders filled in later by t1/finish_env.sh:
# the Keycloak client secret and the LiteLLM key for Open WebUI.
set -euo pipefail
T1_ENV=${T1_ENV:-$HOME/t1-tokenpath-env}
mkdir -p "$T1_ENV/logs"
cd "$T1_ENV"
umask 077
rnd() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }

[ -f compose.env ] || cat > compose.env <<EOF
T1_ENV=$T1_ENV
KC_ADMIN=kcadmin
KC_ADMIN_PASSWORD=$(rnd 18)
PG_PASSWORD=$(rnd 18)
KEYCLOAK_VERSION=26.7.5
OPENWEBUI_VERSION=v0.12.5
EOF

PG_PASSWORD=$(grep '^PG_PASSWORD=' compose.env | cut -d= -f2-)
[ -f litellm.env ] || cat > litellm.env <<EOF
LITELLM_PORT=18011
LITELLM_MASTER_KEY=sk-$(rnd 24)
DATABASE_URL=postgresql://litellm:$PG_PASSWORD@127.0.0.1:15432/litellm
EOF

[ -f accounts.txt ] || cat > accounts.txt <<EOF
admin@corp.example $(rnd 12)
alice@corp.example $(rnd 12)
bob@corp.example $(rnd 12)
carol@corp.example $(rnd 12)
EOF

[ -f openwebui.env ] || cat > openwebui.env <<EOF
PORT=18087
WEBUI_URL=http://127.0.0.1:18087
WEBUI_SECRET_KEY=$(rnd 32)
# Corp SSO (Keycloak). offline_access: Open WebUI keeps an offline refresh token,
# as PR #1561 asks.
ENABLE_OAUTH_SIGNUP=true
OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true
OAUTH_CLIENT_ID=open-webui
OAUTH_CLIENT_SECRET=__OWUI_CLIENT_SECRET__
OPENID_PROVIDER_URL=http://127.0.0.1:18180/realms/corp/.well-known/openid-configuration
OAUTH_PROVIDER_NAME='Corp SSO'
OAUTH_SCOPES='openid email profile offline_access'
DEFAULT_USER_ROLE=user
# One connection to LiteLLM, approach A: system_oauth puts the user's Keycloak
# access token in Authorization; LiteLLM's key goes in x-litellm-api-key. The
# connection's own key is a dummy, to prove it is never what LiteLLM checks.
OPENAI_API_BASE_URLS=http://127.0.0.1:18011/v1
OPENAI_API_KEYS=unused-connection-key
OPENAI_API_CONFIGS='{"0": {"auth_type": "system_oauth", "headers": {"x-litellm-api-key": "__APP_KEY__", "x-memmachine-client": "openwebui", "x-openwebui-chat-id": "{{CHAT_ID}}", "x-openwebui-message-id": "{{MESSAGE_ID}}", "x-openwebui-user-message-id": "{{USER_MESSAGE_ID}}", "x-openwebui-task": "{{TASK}}", "x-openwebui-model-id": "{{MODEL_ID}}"}}}'
# Approach B's signed user header stays off, so the leak checks see A alone.
ENABLE_FORWARD_USER_INFO_HEADERS=false
TASK_MODEL_EXTERNAL=fake
BYPASS_MODEL_ACCESS_CONTROL=true
ENABLE_OLLAMA_API=false
ENABLE_MEMORIES=false
ANONYMIZED_TELEMETRY=false
DO_NOT_TRACK=true
SCARF_NO_ANALYTICS=true
EOF

chmod 600 compose.env litellm.env accounts.txt openwebui.env
echo "T1 env ready in $T1_ENV"
