#!/usr/bin/env bash
# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0
#
# Phase 1, VM-1: add the platform to what the customer runs, then upgrade the hook.
#   1. install the (mock) platform next to MemMachine core
#   2. Keycloak: additions only (client memmachine-platform, employee_id claim on
#      the existing open-webui client)
#   3. register this LiteLLM, Open WebUI's app key and the people with the platform
#   4. upgrade the hook in LiteLLM: preflight, back up, swap, restart; roll back
#      by itself if LiteLLM does not come back
#
#   bash deploy/aws/1-integrate/server.sh
#
# Then copy $ENV_DIR/handoff/client-integrate.env to VM-2 for its phase 1.
# shellcheck source=../lib.sh
. "$(dirname "$0")/../lib.sh"

prepare
[[ -e $STATE/b08-keys.done ]] || die "phase 0 has not finished on this host (0-baseline/server.sh)"
SVC_LABEL=litellm-gateway
P=$ENV_DIR/platform
# shellcheck disable=SC1091
. "$STATE/secrets.env"
admin() { (cd "$REPO" && "$P/.venv/bin/python" -m mock_platform.admin --db "$P/registry.sqlite" "$@"); }

i_install() {
  mkdir -p "$P"
  [[ -x $P/.venv/bin/python ]] || {
    uv venv "$P/.venv" --python 3.12
    VIRTUAL_ENV=$P/.venv uv pip install fastapi uvicorn httpx 'pyjwt[crypto]'; }
}

i_keycloak() {
  # Never PUTs the existing open-webui client: its secret and redirect URIs stay.
  (set -a; . "$ENV_DIR/keycloak.env"; set +a
   KEYCLOAK_PHASE=integrate KEYCLOAK_ADMIN=$KC_BOOTSTRAP_ADMIN_USERNAME \
   KEYCLOAK_ADMIN_PASSWORD=$KC_BOOTSTRAP_ADMIN_PASSWORD \
   SECRETS_OUT=$ENV_DIR/keycloak-clients.env \
     python3 "$REPO/tools/keycloak_setup.py")
}

i_platform() {
  local PE=$ENV_DIR/platform.env L=http://127.0.0.1:4011
  [[ -s $PE ]] || { cp "$REPO/deploy/env/platform.env.example" "$PE"; chmod 600 "$PE"; }
  setenv "$PE" MOCK_PLATFORM_OIDC_CLIENT_SECRET "$(getenv "$ENV_DIR/keycloak-clients.env" PLATFORM_OIDC_CLIENT_SECRET)"
  # A read-only LiteLLM key, so the platform finds people's own keys by itself.
  if unset_val "$(getenv "$PE" MOCK_PLATFORM_LITELLM_KEY)"; then
    local H=(-H "Authorization: Bearer $LITELLM_MASTER_KEY" -H 'Content-Type: application/json')
    curl -sS "${H[@]}" $L/user/new \
      -d '{"user_id":"memmachine-platform","user_role":"proxy_admin_viewer","auto_create_key":false}' >/dev/null
    setenv "$PE" MOCK_PLATFORM_LITELLM_KEY "$(curl -sS "${H[@]}" $L/key/generate \
      -d '{"user_id":"memmachine-platform","key_alias":"memmachine-platform-viewer"}' \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')"
  fi

  # Open WebUI's signing secret: Open WebUI and the platform hold it, LiteLLM never.
  (umask 077
   [[ -s $STATE/owui-jwt-secret ]] || rand 48 >"$STATE/owui-jwt-secret"
   [[ -s $STATE/service-credential ]] || admin service add "$SVC_LABEL" | tail -1 >"$STATE/service-credential")
  grep -q '^mm-svc-' "$STATE/service-credential" || die "no service credential in $STATE/service-credential"

  admin show | grep -q '"open-webui-app"' || (
    OPENWEBUI_APP_KEY=$(getenv "$ENV_DIR/keys.env" OPENWEBUI_APP_KEY) \
    FORWARD_USER_INFO_HEADER_JWT_SECRET=$(cat "$STATE/owui-jwt-secret") \
      admin key app --key-env OPENWEBUI_APP_KEY --label open-webui-app \
        --secret-env FORWARD_USER_INFO_HEADER_JWT_SECRET --issuer open-webui --owner open-webui)
  # Carol is left out: her first chat shows self-enrolment.
  local listing u; listing=$(admin user list)
  for u in "admin@corp.example admin E10000" "alice@corp.example user E12345" "bob@corp.example user E67890"; do
    set -- $u
    grep -q "^$1 " <<<"$listing" || admin user add "$1" --role "$2" --employee-id "$3" >/dev/null
  done
  admin user list

  sudo cp "$REPO/deploy/systemd/ltmplat-platform.service" /etc/systemd/system/
  # On 0.0.0.0 so people reach the portal from VM-2 (the SG lets only VM-2 in).
  # The memory API answers there too, but only to a registered service credential.
  override_execstart ltmplat-platform "$P/.venv/bin/python -m mock_platform.serve" \
    "--host 0.0.0.0 --port \${PLATFORM_PORT}"
  sudo systemctl daemon-reload
  sudo systemctl enable --now ltmplat-platform
  wait_for http://127.0.0.1:8100/healthz 120
  wait_for http://127.0.0.1:8100/portal/api/config 60
}

# ---------------------------------------------------------------- hook upgrade
i_upgrade() {
  local LD=$ENV_DIR/litellm LE=$ENV_DIR/litellm.env cred stage resp backup
  cred=$(cat "$STATE/service-credential")

  # The new hook's settings: core is reached through the platform from now on,
  # with this LiteLLM's credential; the old direct core URL goes away.
  stage=$(mktemp -d)
  cp "$LE" "$stage/litellm.env"
  setenv "$stage/litellm.env" MEMMACHINE_BASE_URL http://127.0.0.1:8100
  setenv "$stage/litellm.env" MEMMACHINE_API_KEY "$cred"
  setenv "$stage/litellm.env" MEMMACHINE_TIMEOUT 60
  # Open WebUI names people by employee ID (OAUTH_USERNAME_CLAIM, VM-2 phase 1).
  setenv "$stage/litellm.env" MEMMACHINE_OWUI_USER_CLAIM name
  cp -r "$REPO/memmachine_hook.py" "$REPO/memmachine_adapters" "$stage/"

  log "preflight 1/3: the platform is up"
  curl -sf http://127.0.0.1:8100/healthz >/dev/null || die "platform not answering on :8100"
  log "preflight 2/3: the platform knows this LiteLLM's credential"
  # A registered credential without a key gets past check 1 and stops at check 2.
  resp=$(curl -s -X POST http://127.0.0.1:8100/api/v2/memories/search \
           -H "Authorization: Bearer $cred" -H 'Content-Type: application/json' -d '{}')
  grep -q 'no virtual key evidence' <<<"$resp" || die "platform refuses the service credential: $resp"
  log "preflight 3/3: the new hook loads with these settings in LiteLLM's Python"
  (set -a; . "$stage/litellm.env"; set +a; cd "$stage" \
     && "$LD/.venv/bin/python" -c 'import memmachine_hook; print("hook loads:", type(memmachine_hook.memmachine_handler).__name__)') \
    || die "the new hook does not load with the new settings; nothing was changed"

  backup=$LD/backup-$(date +%Y%m%d-%H%M%S)
  mkdir -p "$backup"
  cp -a "$LD/memmachine_hook.py" "$LE" "$LD/config.yaml" "$backup/"
  [[ -d $LD/memmachine_adapters ]] && cp -a "$LD/memmachine_adapters" "$backup/"
  echo "backup: $backup"

  log "swap: hook files and settings"
  rm -rf "$LD/memmachine_adapters"
  cp -r "$stage/memmachine_hook.py" "$stage/memmachine_adapters" "$LD/"
  install -m 600 "$stage/litellm.env" "$LE"
  rm -rf "$stage"
  # config.yaml is unchanged: same callback name, same memmachine- aliases.

  sudo systemctl restart ltmplat-litellm
  local t=0
  local failed
  until curl -sf http://127.0.0.1:4011/health/liveliness >/dev/null 2>&1; do
    (( t += 3 ))
    failed=0
    (( t >= 180 )) && failed=1
    # Crashed at start-up (e.g. the hook raised): systemd is restarting it.
    (( t > 30 )) && ! systemctl is-active -q ltmplat-litellm && failed=1
    if (( failed )); then
      log "LiteLLM did not come back: rolling back to $backup"
      rm -rf "$LD/memmachine_adapters"
      cp -a "$backup/memmachine_hook.py" "$backup/config.yaml" "$LD/"
      [[ -d $backup/memmachine_adapters ]] && cp -a "$backup/memmachine_adapters" "$LD/"
      install -m 600 "$backup/litellm.env" "$LE"
      sudo systemctl restart ltmplat-litellm
      wait_for http://127.0.0.1:4011/health/liveliness 180
      die "upgrade rolled back; the old hook is running. See: journalctl -u ltmplat-litellm"
    fi
    sleep 3
  done
  echo "LiteLLM is back with the new hook"

  # Alice's key: the platform links it to her on first use (owner E12345 = her employee ID).
  local key; key=$(getenv "$ENV_DIR/keys.env" ALICE_VIRTUAL_KEY)
  curl -sf -H "Authorization: Bearer $key" -H 'Content-Type: application/json' \
    http://127.0.0.1:4011/v1/chat/completions \
    -d '{"model":"memmachine-glm-5.2","messages":[{"role":"user","content":"Say OK."}]}' \
    | grep -q '"choices"' || die "memmachine-glm-5.2 with Alice's key failed after the upgrade"
  # A 200 alone proves little (memory fails open); the platform must have linked her key.
  admin user list | grep '^alice@corp.example ' | grep -q 'alice-personal-e12345' \
    || die "chat answered, but the platform did not link Alice's key: no memory for her (journalctl -u ltmplat-platform)"
  echo "platform linked Alice's key on its first use: memory goes through the platform"
}

# Core is on 127.0.0.1 already, so only this host reaches it. With LiteLLM on the
# same host that is no boundary; on separate hosts, core would listen only for the
# platform. Note it rather than pretend.
i_handoff() {
  (umask 077; cat >"$HANDOFF/client-integrate.env" <<EOF
FORWARD_USER_INFO_HEADER_JWT_SECRET=$(cat "$STATE/owui-jwt-secret")
EOF
)
}

step i00-install  i_install
step i01-keycloak i_keycloak
step i02-platform i_platform
step i03-upgrade  i_upgrade
i_handoff

cat <<EOF

==============================================================================
 Phase 1, VM-1 done: platform up, hook upgraded (backup in $ENV_DIR/litellm/backup-*).
 Portal: http://127.0.0.1:8100/portal/ (through the tunnel to VM-2, after its phase 1)
 Next: copy $HANDOFF/client-integrate.env to VM-2 and run 1-integrate/client.sh there.
==============================================================================
EOF
