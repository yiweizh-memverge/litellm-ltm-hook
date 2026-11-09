#!/usr/bin/env bash
# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0
#
# Phase 0, VM-1: what the customer already runs before we arrive.
#   - Keycloak: realm "corp", users with an employee_id attribute, client "open-webui"
#   - MemMachine core (Postgres + pgvector, Qdrant), reached directly by...
#   - LiteLLM with virtual keys and the OLD memory hook (the `main` branch's),
#     which talks to core directly and names people by their key's email
# Nothing of the platform yet: that is phase 1.
#
#   CLIENT_IP=<VM-2 private IP> DEEPINFRA_API_KEY=... OPENAI_API_KEY=... \
#     bash deploy/aws/0-baseline/server.sh
#
# Optional: DEMO_PASSWORD (one Keycloak password for everyone; default random each),
# OLD_HOOK_REF (git ref of the old hook, default main),
# EXTRA_USERS (more Corp SSO people, space-separated email:password:employee_id,
#   e.g. "yu.xu@memverge.com:<password>:E13579"; employee IDs are E + 5 digits).
#   Each gets a personal LiteLLM key owned by their employee ID, in keys.env as
#   <LOCALPART>_VIRTUAL_KEY (yu.xu -> YUXU_VIRTUAL_KEY). They are not enrolled with
#   the platform: their first chat enrols them, like carol. Applied on every run,
#   so add people by rerunning.
# Rerunnable: finished steps are skipped (markers in $ENV_DIR/.state).
# shellcheck source=../lib.sh
. "$(dirname "$0")/../lib.sh"

: "${CLIENT_IP:?set CLIENT_IP to the private IP of VM-2, where Open WebUI runs}"
OLD_HOOK_REF=${OLD_HOOK_REF:-main}
SERVER_IP=$(private_ip)
prepare
echo "$CLIENT_IP" >"$STATE/client_ip"   # for 1-integrate/verify.sh

b_packages() {
  sudo apt-get update -q
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq \
    git curl openssl python3 postgresql openjdk-21-jre-headless
  local pg; pg=$(ls /usr/lib/postgresql | sort -n | tail -1)
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq "postgresql-$pg-pgvector"
  install_uv
}

b_install() {
  cd "$ENV_DIR"
  mkdir -p bin litellm keycloak memmachine data/memmachine data/qdrant
  [[ -x litellm/.venv/bin/litellm ]] || {
    uv venv litellm/.venv --python 3.12
    VIRTUAL_ENV=litellm/.venv uv pip install "litellm[proxy]==$LITELLM_VERSION" prisma; }
  local schema
  schema=$(litellm/.venv/bin/python -c "import litellm,os; print(os.path.join(os.path.dirname(litellm.__file__),'proxy','schema.prisma'))")
  PATH=$ENV_DIR/litellm/.venv/bin:$PATH litellm/.venv/bin/prisma generate --schema "$schema"

  local kc=keycloak-$KEYCLOAK_VERSION url=https://github.com/keycloak/keycloak/releases/download/$KEYCLOAK_VERSION
  if [[ ! -d keycloak/$kc ]]; then
    (cd keycloak
     curl -sSLO "$url/$kc.tar.gz"
     want=$(curl -sSL "$url/$kc.tar.gz.sha1" | cut -c1-40)
     got=$(sha1sum "$kc.tar.gz" | cut -c1-40)
     [[ $want == "$got" ]] || die "keycloak sha1 mismatch: $got != $want"
     tar xzf "$kc.tar.gz" && rm "$kc.tar.gz")
  fi

  install_core "$ENV_DIR/memmachine"
  [[ -x bin/qdrant ]] || (cd bin && curl -sSL \
    "https://github.com/qdrant/qdrant/releases/download/v$QDRANT_VERSION/qdrant-x86_64-unknown-linux-gnu.tar.gz" | tar xz)
}

b_secrets() {
  : "${DEEPINFRA_API_KEY:?set DEEPINFRA_API_KEY (chat model)}"
  : "${OPENAI_API_KEY:?set OPENAI_API_KEY (embeddings)}"
  local L=$ENV_DIR/litellm.env
  [[ -s $STATE/secrets.env ]] || (umask 077; printf '%s\n' \
    "LITELLM_MASTER_KEY=sk-$(rand 24)" "LITELLM_DB_PASSWORD=$(rand 24)" \
    "MEMMACHINE_DB_PASSWORD=$(rand 24)" "KC_ADMIN_PASSWORD=$(rand 18)" >"$STATE/secrets.env")
  # shellcheck disable=SC1091
  . "$STATE/secrets.env"

  # The customer's LiteLLM env, with the old hook's settings (core, directly).
  (umask 077; cat >"$L" <<EOF
LITELLM_PORT=4011
LITELLM_MASTER_KEY=$LITELLM_MASTER_KEY
DATABASE_URL=postgresql://ltmplatform:$LITELLM_DB_PASSWORD@127.0.0.1:5432/ltmplatform
STORE_MODEL_IN_DB=False
DEEPINFRA_API_KEY=$DEEPINFRA_API_KEY
OPENAI_API_KEY=$OPENAI_API_KEY

# MemMachine memory hook, v1: talks to MemMachine core directly.
MEMMACHINE_BASE_URL=http://127.0.0.1:8090
MEMMACHINE_PROJECT_ID=default
MEMMACHINE_MODEL_PREFIX=memmachine-
MEMMACHINE_TIMEOUT=30
EOF
)
  cp "$REPO/deploy/env/keycloak.env.example" "$ENV_DIR/keycloak.env"
  setenv "$ENV_DIR/keycloak.env" KC_BOOTSTRAP_ADMIN_PASSWORD "$KC_ADMIN_PASSWORD"
  cp "$REPO/deploy/env/memmachine.env.example" "$ENV_DIR/memmachine.env"
  cp "$REPO/deploy/memmachine-cfg.yml" "$ENV_DIR/memmachine/cfg.yml"
  replace "$ENV_DIR/memmachine/cfg.yml" "<LITELLM_MASTER_KEY, step 4>" "$LITELLM_MASTER_KEY"
  replace "$ENV_DIR/memmachine/cfg.yml" "<MEMMACHINE_DB_PASSWORD, step 3>" "$MEMMACHINE_DB_PASSWORD"
  if [[ ! -s $ENV_DIR/accounts.txt ]]; then
    for u in admin alice bob carol; do
      echo "$u@corp.example ${DEMO_PASSWORD:-$(rand 9)}"
    done >"$ENV_DIR/accounts.txt"
  fi
  chmod 600 "$ENV_DIR"/*.env "$ENV_DIR/accounts.txt" "$ENV_DIR/memmachine/cfg.yml"
}

b_postgres() {
  # shellcheck disable=SC1091
  . "$STATE/secrets.env"
  sudo systemctl enable --now postgresql
  psql_() { sudo -u postgres psql -v ON_ERROR_STOP=1 -tAc "$@"; }
  for spec in "ltmplatform:$LITELLM_DB_PASSWORD" "memmachine:$MEMMACHINE_DB_PASSWORD"; do
    local role=${spec%%:*} pass=${spec#*:}
    if [[ $(psql_ "SELECT 1 FROM pg_roles WHERE rolname='$role'") == 1 ]]; then
      psql_ "ALTER USER $role WITH PASSWORD '$pass';"
    else
      psql_ "CREATE USER $role WITH PASSWORD '$pass';"
    fi
    [[ $(psql_ "SELECT 1 FROM pg_database WHERE datname='$role'") == 1 ]] || psql_ "CREATE DATABASE $role OWNER $role;"
  done
  sudo -u postgres psql -v ON_ERROR_STOP=1 -d memmachine -c "CREATE EXTENSION IF NOT EXISTS vector;"
}

b_units() {
  # Only what the customer runs; the platform's unit arrives in phase 1.
  local u
  for u in keycloak litellm qdrant memmachine; do
    sudo cp "$REPO/deploy/systemd/ltmplat-$u.service" /etc/systemd/system/
  done
  # Keycloak and LiteLLM serve VM-2 too. Keycloak's URL stays http://127.0.0.1:8180
  # everywhere: VM-2 forwards that port here, your laptop tunnels it.
  local kc=$ENV_DIR/keycloak/keycloak-$KEYCLOAK_VERSION
  override_execstart ltmplat-keycloak "$kc/bin/kc.sh start-dev --http-host=0.0.0.0 --http-port=8180" \
    "--hostname=http://127.0.0.1:8180 --health-enabled=true"
  override_execstart ltmplat-litellm "$ENV_DIR/litellm/.venv/bin/litellm" \
    "--config $ENV_DIR/litellm/config.yaml --host 0.0.0.0 --port \${LITELLM_PORT}"
  sudo systemctl daemon-reload
}

b_keycloak() {
  sudo systemctl enable --now ltmplat-keycloak
  wait_for http://127.0.0.1:8180/realms/master/.well-known/openid-configuration 300
  (set -a; . "$ENV_DIR/keycloak.env"; set +a
   KEYCLOAK_PHASE=baseline KEYCLOAK_ADMIN=$KC_BOOTSTRAP_ADMIN_USERNAME \
   KEYCLOAK_ADMIN_PASSWORD=$KC_BOOTSTRAP_ADMIN_PASSWORD \
   ACCOUNTS_FILE=$ENV_DIR/accounts.txt SECRETS_OUT=$ENV_DIR/keycloak-clients.env \
   OWUI_URLS="http://127.0.0.1:8087,http://$CLIENT_IP:8087" \
     python3 "$REPO/tools/keycloak_setup.py")
}

# More people in the realm, through keycloak_setup.py's own ensure_user (same
# attributes as the built-in four). Passwords never touch a file.
b_extra_users() {
  (set -a; . "$ENV_DIR/keycloak.env"; set +a
   KEYCLOAK_ADMIN=$KC_BOOTSTRAP_ADMIN_USERNAME KEYCLOAK_ADMIN_PASSWORD=$KC_BOOTSTRAP_ADMIN_PASSWORD \
     python3 - "$REPO/tools/keycloak_setup.py" $EXTRA_USERS <<'PY'
import importlib.util, re, sys
spec = importlib.util.spec_from_file_location("keycloak_setup", sys.argv[1])
ks = importlib.util.module_from_spec(spec); spec.loader.exec_module(ks)
token = ks.admin_token()
for entry in sys.argv[2:]:
    email, password, emp = (entry.split(":", 2) + ["", ""])[:3]
    if not (email and password and re.fullmatch(r"E[0-9]{5}", emp)):
        sys.exit(f"EXTRA_USERS entry {email or entry!r}: need email:password:E12345")
    first, _, last = email.split("@")[0].partition(".")
    ks.ensure_user(token, email, first.capitalize(), last.capitalize() or "-", emp, password)
PY
  )
}

b_core() {
  sudo systemctl enable --now ltmplat-qdrant
  wait_for http://127.0.0.1:6333/ 120
  sudo systemctl enable --now ltmplat-memmachine
  wait_for http://127.0.0.1:8090/openapi.json 300
  core_roundtrip install-check   # needs LiteLLM (b06) for the embedder
}

b_litellm() {
  # The old hook, as a customer would have dropped it next to the config.
  git -C "$REPO" show "$OLD_HOOK_REF:memmachine_hook.py" >"$ENV_DIR/litellm/memmachine_hook.py" \
    || die "cannot read memmachine_hook.py at $OLD_HOOK_REF (set OLD_HOOK_REF)"
  cp "$REPO/deploy/litellm-config.yaml" "$ENV_DIR/litellm/config.yaml"
  sudo systemctl enable --now ltmplat-litellm
  wait_for http://127.0.0.1:4011/health/liveliness 600   # first start migrates its tables
  # shellcheck disable=SC1091
  . "$STATE/secrets.env"
  local H=(-H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json')
  curl -sf "${H[@]}" http://127.0.0.1:4011/v1/embeddings \
    -d '{"model":"text-embedding-3-small","input":"hello"}' | grep -q '"embedding"' \
    || die "embedding through LiteLLM failed: check OPENAI_API_KEY"
  curl -sf "${H[@]}" http://127.0.0.1:4011/v1/chat/completions \
    -d '{"model":"glm-5.2","messages":[{"role":"user","content":"Say OK."}]}' | grep -q '"choices"' \
    || die "chat through LiteLLM failed: check DEEPINFRA_API_KEY"
}

# LiteLLM admin calls with the master key.
lt() {
  # shellcheck disable=SC1091
  . "$STATE/secrets.env"
  curl -sS -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json' \
    "http://127.0.0.1:4011$1" -d "$2"
}
newkey() {  # newkey USER_JSON KEY_JSON: prints the new key
  lt /user/new "$1" >/dev/null
  lt /key/generate "$2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])'
}

b_keys() {
  local KF=$ENV_DIR/keys.env
  [[ -f $KF ]] || { touch "$KF"; chmod 600 "$KF"; }
  # Open WebUI's one shared key. The old hook names people by the key owner's
  # email, so every Open WebUI user shares this one memory: the problem phase 1 fixes.
  unset_val "$(getenv "$KF" OPENWEBUI_APP_KEY)" && setenv "$KF" OPENWEBUI_APP_KEY "$(newkey \
    '{"user_id":"open-webui","user_email":"open-webui@corp.example","user_role":"internal_user_viewer","auto_create_key":false}' \
    '{"user_id":"open-webui","key_alias":"open-webui-app"}')"
  setenv "$KF" OPENWEBUI_APP_OWNER open-webui
  for p in "E12345 alice ALICE" "E67890 bob BOB" "E24680 carol CAROL"; do
    set -- $p
    unset_val "$(getenv "$KF" "${3}_VIRTUAL_KEY")" || continue
    setenv "$KF" "${3}_VIRTUAL_KEY" "$(newkey \
      "{\"user_id\":\"$1\",\"user_email\":\"$2@corp.example\",\"user_role\":\"internal_user\",\"auto_create_key\":false}" \
      "{\"user_id\":\"$1\",\"key_alias\":\"$2-personal-${1,,}\"}")"
  done
}

# A personal LiteLLM key for each EXTRA_USERS person, owned by their employee ID
# like Alice's, in keys.env as <LOCALPART>_VIRTUAL_KEY (yu.xu -> YUXU_VIRTUAL_KEY).
# Runs on every invocation; someone already in keys.env is left alone. A key under
# the same alias that keys.env lacks cannot be read back from LiteLLM (it keeps a
# hash), so it is deleted and made again.
b_extra_keys() {
  local KF=$ENV_DIR/keys.env entry email emp local_part var alias
  for entry in $EXTRA_USERS; do
    email=${entry%%:*} emp=${entry##*:}
    local_part=${email%%@*}
    var=$(tr -cd 'A-Za-z0-9' <<<"$local_part" | tr a-z A-Z)_VIRTUAL_KEY
    alias="$local_part-personal-${emp,,}"
    if ! unset_val "$(getenv "$KF" "$var")"; then
      echo "$email: $var already in keys.env"
      continue
    fi
    lt /key/delete "{\"key_aliases\":[\"$alias\"]}" >/dev/null || true
    setenv "$KF" "$var" "$(newkey \
      "{\"user_id\":\"$emp\",\"user_email\":\"$email\",\"user_role\":\"internal_user\",\"auto_create_key\":false}" \
      "{\"user_id\":\"$emp\",\"key_alias\":\"$alias\"}")"
    grep -q "^$var=sk-" "$KF" || die "no key made for $email"
    echo "$email: $var added to keys.env (key alias $alias, owner $emp)"
  done
}

b_check() {
  # The old hook answers a memmachine- model with Alice's key.
  local key; key=$(getenv "$ENV_DIR/keys.env" ALICE_VIRTUAL_KEY)
  curl -sf -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
    http://127.0.0.1:4011/v1/chat/completions \
    -d '{"model":"memmachine-glm-5.2","messages":[{"role":"user","content":"Say OK."}]}' \
    | grep -q '"choices"' || die "memmachine-glm-5.2 with the old hook failed: journalctl -u ltmplat-litellm"
  echo "old hook: memmachine-glm-5.2 answers"
}

b_handoff() {
  # What VM-2 needs from here. Copy it over (README), then delete it there.
  local KF=$ENV_DIR/keys.env
  (umask 077; cat >"$HANDOFF/client-baseline.env" <<EOF
SERVER_IP=$SERVER_IP
OAUTH_CLIENT_SECRET=$(getenv "$ENV_DIR/keycloak-clients.env" OWUI_OIDC_CLIENT_SECRET)
OPENWEBUI_APP_KEY=$(getenv "$KF" OPENWEBUI_APP_KEY)
ALICE_VIRTUAL_KEY=$(getenv "$KF" ALICE_VIRTUAL_KEY)
ADMIN_PASSWORD=$(awk '$1=="admin@corp.example"{print $2}' "$ENV_DIR/accounts.txt")
EOF
)
}

step b00-packages b_packages
step b01-install  b_install
step b02-secrets  b_secrets
step b03-postgres b_postgres
step b04-units    b_units
step b05-keycloak b_keycloak
[[ -n ${EXTRA_USERS:-} ]] && { log "b05b-extra-users"; b_extra_users; }
step b06-litellm  b_litellm
step b07-core     b_core
step b08-keys     b_keys
[[ -n ${EXTRA_USERS:-} ]] && { log "b08b-extra-keys"; b_extra_keys; }
log "b09-check"; b_check
b_handoff

cat <<EOF

==============================================================================
 Phase 0, VM-1 is up: Keycloak, MemMachine core, LiteLLM with the OLD hook.
 Next: copy the handoff to VM-2 and run its phase 0 (README section 3):

   $HANDOFF/client-baseline.env
==============================================================================
EOF
