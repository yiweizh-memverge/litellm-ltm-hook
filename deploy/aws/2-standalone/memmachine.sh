#!/usr/bin/env bash
# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0
#
# Phase 2, VM-2 (or any fresh host): MemMachine core on its own, timed, to see
# what installing it takes. Not connected to the demo stack: nothing points at it.
#
#   LITELLM_URL=http://<VM-1 private IP>:4011 LITELLM_KEY=sk-... \
#     bash deploy/aws/2-standalone/memmachine.sh
#
# Core needs an OpenAI-compatible endpoint with a chat model (glm-5.2) and an
# embedding model (text-embedding-3-small): here VM-1's LiteLLM, with a key made
# for it (README, phase 2).
# Pieces: Postgres + pgvector, Qdrant, core itself (git + uv), one config file.
# shellcheck source=../lib.sh
. "$(dirname "$0")/../lib.sh"

: "${LITELLM_URL:?set LITELLM_URL, e.g. http://<VM-1 private IP>:4011}"
: "${LITELLM_KEY:?set LITELLM_KEY: a LiteLLM key for the embedder and LLM of core}"
prepare
T0=$SECONDS

s_packages() {
  sudo apt-get update -q
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq git curl python3 postgresql
  local pg; pg=$(ls /usr/lib/postgresql | sort -n | tail -1)
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq "postgresql-$pg-pgvector"
  install_uv
}

s_postgres() {
  (umask 077; [[ -s $STATE/mm-db-password ]] || rand 24 >"$STATE/mm-db-password")
  local pw; pw=$(cat "$STATE/mm-db-password")
  sudo systemctl enable --now postgresql
  psql_() { sudo -u postgres psql -v ON_ERROR_STOP=1 -tAc "$@"; }
  if [[ $(psql_ "SELECT 1 FROM pg_roles WHERE rolname='memmachine'") == 1 ]]; then
    psql_ "ALTER USER memmachine WITH PASSWORD '$pw';"
  else
    psql_ "CREATE USER memmachine WITH PASSWORD '$pw';"
  fi
  [[ $(psql_ "SELECT 1 FROM pg_database WHERE datname='memmachine'") == 1 ]] || psql_ "CREATE DATABASE memmachine OWNER memmachine;"
  sudo -u postgres psql -v ON_ERROR_STOP=1 -d memmachine -c "CREATE EXTENSION IF NOT EXISTS vector;"
}

s_qdrant() {
  mkdir -p "$ENV_DIR/bin" "$ENV_DIR/data/qdrant"
  [[ -x $ENV_DIR/bin/qdrant ]] || (cd "$ENV_DIR/bin" && curl -sSL \
    "https://github.com/qdrant/qdrant/releases/download/v$QDRANT_VERSION/qdrant-x86_64-unknown-linux-gnu.tar.gz" | tar xz)
}

s_core() {
  mkdir -p "$ENV_DIR/memmachine" "$ENV_DIR/data/memmachine"
  install_core "$ENV_DIR/memmachine"
}

s_config() {
  local C=$ENV_DIR/memmachine/cfg.yml
  cp "$REPO/deploy/memmachine-cfg.yml" "$C"
  replace "$C" "<LITELLM_MASTER_KEY, step 4>" "$LITELLM_KEY"
  replace "$C" "<MEMMACHINE_DB_PASSWORD, step 3>" "$(cat "$STATE/mm-db-password")"
  replace "$C" "http://127.0.0.1:4011/v1" "${LITELLM_URL%/}/v1"
  cp "$REPO/deploy/env/memmachine.env.example" "$ENV_DIR/memmachine.env"
  chmod 600 "$C" "$ENV_DIR/memmachine.env"
  local u
  for u in qdrant memmachine; do sudo cp "$REPO/deploy/systemd/ltmplat-$u.service" /etc/systemd/system/; done
  sudo systemctl daemon-reload
}

s_start() {
  sudo systemctl enable --now ltmplat-qdrant
  wait_for http://127.0.0.1:6333/ 120
  sudo systemctl enable --now ltmplat-memmachine
  wait_for http://127.0.0.1:8090/openapi.json 300
}


step s00-packages  s_packages
step s01-postgres  s_postgres
step s02-qdrant    s_qdrant
step s03-core      s_core
step s04-config    s_config
step s05-start     s_start
log "s06-roundtrip"; core_roundtrip standalone-test

echo
echo "=============================================================================="
echo " Standalone MemMachine core is up on 127.0.0.1:8090. Steps, in seconds:"
grep '^s0' "$STATE/timings" | awk '{printf "   %-14s %5d\n", $1, $2}'
echo "   total this run  $(( SECONDS - T0 ))"
echo " Pieces installed: Postgres + pgvector, Qdrant, core (git clone + uv sync),"
echo " cfg.yml (DB password, model endpoint + key), two systemd units."
echo "=============================================================================="
