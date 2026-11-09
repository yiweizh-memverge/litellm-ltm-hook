# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0
#
# Shared by the deploy/aws scripts. Source it; don't run it.
set -euo pipefail

ENV_DIR=${ENV_DIR:-/opt/ltm-platform-env}
REPO=$ENV_DIR/repo
STATE=$ENV_DIR/.state
HANDOFF=$ENV_DIR/handoff
export PATH="$HOME/.local/bin:$PATH"

# Versions SETUP.md was tested with.
LITELLM_VERSION=1.100.1
OPENWEBUI_VERSION=0.11.3
KEYCLOAK_VERSION=26.7.5
# MemMachine core: main of 2026-11-02 (v0.4.0 + 10 fixes). The single-host SETUP.md
# still pins speedkick 0b15fad; core's REST API is the same for what the hook,
# platform and portal call (checked endpoint by endpoint against both).
MEMMACHINE_BRANCH=main
MEMMACHINE_COMMIT=377510d
QDRANT_VERSION=1.19.0

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
rand() { python3 -c "import secrets; print(secrets.token_urlsafe($1))"; }

# setenv FILE KEY VALUE: replace the first uncommented KEY= line, or append one.
setenv() {
  python3 - "$@" <<'PY'
import sys
path, key, val = sys.argv[1:]
lines = open(path).read().splitlines()
for i, line in enumerate(lines):
    if line.startswith(key + "="):
        lines[i] = f"{key}={val}"
        break
else:
    lines.append(f"{key}={val}")
open(path, "w").write("\n".join(lines) + "\n")
PY
}
getenv() { grep -m1 "^$2=" "$1" 2>/dev/null | cut -d= -f2- || true; }
# True if the value is still a template placeholder (<...>) or empty.
unset_val() { [[ -z "$1" || "$1" == \<* ]]; }
# replace FILE OLD NEW: literal string replace.
replace() { python3 - "$@" <<'PY'
import sys
path, old, new = sys.argv[1:]
s = open(path).read()
open(path, "w").write(s.replace(old, new))
PY
}

wait_for() {  # wait_for URL [seconds]
  local url=$1 limit=${2:-300} t=0
  until curl -sf "$url" >/dev/null 2>&1; do
    (( t += 3 )); (( t < limit )) || die "$url not up after ${limit}s"
    sleep 3
  done
}

# step NAME FUNCTION: run once; a marker in $STATE skips it on reruns.
step() {
  if [[ -e $STATE/$1.done ]]; then log "$1: done before, skipping"; return; fi
  log "$1"
  local t0=$SECONDS
  "$2"
  touch "$STATE/$1.done"
  echo "($1 took $(( SECONDS - t0 ))s)"
  echo "$1 $(( SECONDS - t0 ))" >>"$STATE/timings"
}

# Bind a stock unit to another address: a drop-in that replaces its ExecStart.
override_execstart() {  # override_execstart UNIT COMMAND...
  local unit=$1; shift
  sudo mkdir -p "/etc/systemd/system/$unit.service.d"
  printf '[Service]\nExecStart=\nExecStart=%s\n' "$*" \
    | sudo tee "/etc/systemd/system/$unit.service.d/bind.conf" >/dev/null
}

private_ip() {
  local tok
  tok=$(curl -sf -X PUT http://169.254.169.254/latest/api/token \
          -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null || true)
  curl -sf -H "X-aws-ec2-metadata-token: $tok" \
    http://169.254.169.254/latest/meta-data/local-ipv4 2>/dev/null \
    || hostname -I | awk '{print $1}'
}

install_uv() { command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh; }

# MemMachine core into DIR/src. The qdrant extra is not optional for this config
# (cfg.yml keeps vectors in Qdrant): without qdrant-client core still starts and
# serves /openapi.json, but every memory call fails with HTTP 500.
install_core() {  # install_core DIR
  [[ -x $1/src/.venv/bin/memmachine-server ]] \
    && [[ $(git -C "$1/src" rev-parse --short=7 HEAD) == "$MEMMACHINE_COMMIT" ]] \
    && "$1/src/.venv/bin/python" -c 'import qdrant_client' 2>/dev/null && return
  [[ -d $1/src ]] || git clone -b "$MEMMACHINE_BRANCH" https://github.com/MemMachine/MemMachine.git "$1/src"
  (cd "$1/src" && { git cat-file -e "$MEMMACHINE_COMMIT^{commit}" 2>/dev/null \
                    || git fetch -q origin "$MEMMACHINE_BRANCH"; } \
     && git -c advice.detachedHead=false checkout "$MEMMACHINE_COMMIT" \
     && uv sync --package memmachine-server --extra qdrant)
}

# Store one fact straight in core, then search for it. Proves Postgres, Qdrant and
# the embedder (through LiteLLM) all work; /openapi.json answering proves none of it.
core_roundtrip() {  # core_roundtrip ORG_ID
  local M=http://127.0.0.1:8090/api/v2/memories st found t=0 add search
  add='{"org_id":"'$1'","project_id":"default","types":[],"messages":[{"content":"The install check codename is Heron.","producer":"installer","produced_for":"litellm","role":"user"}]}'
  search='{"org_id":"'$1'","project_id":"default","query":"What is the codename?","top_k":5,"types":[]}'
  st=$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d "$add" "$M")
  echo "core add: HTTP $st"
  [[ $st == 2* ]] || die "core refused the add, HTTP $st: journalctl -u ltmplat-memmachine"
  until found=$(curl -s -H 'Content-Type: application/json' -d "$search" "$M/search") \
        && grep -q Heron <<<"$found"; do
    (( t += 5 )); (( t < 90 )) || die "stored, but search did not return it within 90s: $found"
    sleep 5
  done
  echo "core search: found it (stores are indexed asynchronously; took ~${t}s)"
}

prepare() {
  [[ $(id -un) == ubuntu ]] || die "run as user ubuntu (the systemd units run services as ubuntu)"
  [[ -f $REPO/memmachine_hook.py ]] || die "the repo is not at $REPO (see deploy/aws/README.md)"
  sudo mkdir -p "$ENV_DIR" && sudo chown ubuntu:ubuntu "$ENV_DIR"
  mkdir -p "$STATE" "$HANDOFF" && chmod 700 "$STATE" "$HANDOFF"
}

# Open WebUI makes its first user an admin: sign admin@ in before anyone else.
owui_admin_first() {  # owui_admin_first OWUI_URL PASSWORD
  python3 - "$REPO/tools" "$1" "$2" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from sso_browser import cookie, sso_login
opener, _ = sso_login(sys.argv[2] + "/oauth/oidc/login", "admin@corp.example", sys.argv[3])
assert cookie(opener, "token"), "admin@ could not sign in to Open WebUI"
print("Open WebUI: admin@corp.example signed in (Open WebUI admin)")
PY
}
