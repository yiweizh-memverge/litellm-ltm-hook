#!/usr/bin/env bash
# After Keycloak and LiteLLM are up: create the corp realm, Open WebUI's shared
# LiteLLM key, and fill both into $T1_ENV/openwebui.env. Idempotent.
#
#   t1/finish_env.sh        (from the repo root)
set -euo pipefail
T1_ENV=${T1_ENV:-$HOME/t1-tokenpath-env}
REPO=$(cd "$(dirname "$0")/.." && pwd)
set -a; . "$T1_ENV/compose.env"; . "$T1_ENV/litellm.env"; set +a

# 1. Realm "corp", clients open-webui and memmachine-platform, users from accounts.txt.
KEYCLOAK_URL=http://127.0.0.1:18180 \
KEYCLOAK_ADMIN=$KC_ADMIN KEYCLOAK_ADMIN_PASSWORD=$KC_ADMIN_PASSWORD \
OWUI_URLS=http://127.0.0.1:18087,http://localhost:18087 \
PORTAL_URLS=http://127.0.0.1:18100,http://localhost:18100 \
ACCOUNTS_FILE=$T1_ENV/accounts.txt SECRETS_OUT=$T1_ENV/keycloak-clients.env \
  python3 "$REPO/tools/keycloak_setup.py"
OWUI_SECRET=$(grep '^OWUI_OIDC_CLIENT_SECRET=' "$T1_ENV/keycloak-clients.env" | cut -d= -f2-)

# 2. Open WebUI's one shared LiteLLM key, owned by a service user (never a person).
L=http://127.0.0.1:$LITELLM_PORT
H=(-s -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json")
if [ ! -f "$T1_ENV/keys.env" ]; then
  curl "${H[@]}" "$L/user/new" -d '{"user_id": "open-webui", "user_role": "internal_user_viewer", "auto_create_key": false}' >/dev/null
  APP_KEY=$(curl "${H[@]}" "$L/key/generate" -d '{"user_id": "open-webui", "key_alias": "open-webui-app"}' \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')
  (umask 077; echo "OPENWEBUI_APP_KEY=$APP_KEY" > "$T1_ENV/keys.env")
fi
APP_KEY=$(grep '^OPENWEBUI_APP_KEY=' "$T1_ENV/keys.env" | cut -d= -f2-)

# 3. Fill the placeholders.
sed -i -e "s|__OWUI_CLIENT_SECRET__|$OWUI_SECRET|" -e "s|__APP_KEY__|$APP_KEY|" "$T1_ENV/openwebui.env"
echo "openwebui.env filled; start or restart Open WebUI next."
