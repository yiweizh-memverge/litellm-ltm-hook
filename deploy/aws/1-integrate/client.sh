#!/usr/bin/env bash
# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0
#
# Phase 1, VM-2: the frontends' side of the integration. Run after phase 1 on VM-1,
# with the handoff file it wrote copied here:
#
#   bash deploy/aws/1-integrate/client.sh [path/to/client-integrate.env]
#
# Open WebUI (four env lines, one restart; Open WebUI 0.11.3 reads these from the
# environment on every start, unlike most of its settings):
#   ENABLE_FORWARD_USER_INFO_HEADERS / FORWARD_USER_INFO_HEADER_JWT_SECRET
#       every request to LiteLLM carries a token naming the user, signed with a
#       secret only Open WebUI and the platform hold
#   OAUTH_USERNAME_CLAIM=employee_id / OAUTH_UPDATE_NAME_ON_LOGIN=true
#       the user's name becomes their employee ID, the identity the platform checks
# Claude Code: the headers its adapter needs, merged into ~/.claude/settings.json.
# shellcheck source=../lib.sh
. "$(dirname "$0")/../lib.sh"

prepare
[[ -e $STATE/c04-openwebui.done ]] || die "phase 0 has not finished on this host (0-baseline/client.sh)"
IN=${1:-$HANDOFF/client-integrate.env}
[[ -s $IN ]] || die "no handoff file $IN: copy VM-1's $HANDOFF/client-integrate.env here"
# shellcheck disable=SC1090
. "$IN"
: "${FORWARD_USER_INFO_HEADER_JWT_SECRET:?}"

j_openwebui() {
  local O=$ENV_DIR/openwebui.env
  cp -a "$O" "$O.before-memmachine"
  setenv "$O" ENABLE_FORWARD_USER_INFO_HEADERS true
  setenv "$O" FORWARD_USER_INFO_HEADER_JWT_SECRET "$FORWARD_USER_INFO_HEADER_JWT_SECRET"
  setenv "$O" OAUTH_USERNAME_CLAIM employee_id
  setenv "$O" OAUTH_UPDATE_NAME_ON_LOGIN true
  echo "--- openwebui.env changes:"
  diff "$O.before-memmachine" "$O" | sed 's/\(JWT_SECRET=\).*/\1<secret>/' || true
  sudo systemctl restart ltmplat-openwebui
  wait_for http://127.0.0.1:8087/api/config 300
}

j_claude_code() {
  local S=~/.claude/settings.json
  [[ -s $S ]] || die "$S missing (phase 0 writes it)"
  cp -a "$S" "$S.before-memmachine"
  python3 - "$S" <<'PY'
import json, sys
path = sys.argv[1]
s = json.load(open(path))
env = s.setdefault("env", {})
env.update({
    # The adapter's turn model is built on these; without them, no memory.
    "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
    # The project, and the frontend's name so the hook picks its Claude Code adapter.
    "ANTHROPIC_CUSTOM_HEADERS": "x-memmachine-project: default\nx-memmachine-client: claude-code",
    # Room for the memory block.
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "192000",
})
s["autoMemoryEnabled"] = False   # Claude Code's own memory files stay out of the way
json.dump(s, open(path, "w"), indent=2)
PY
  chmod 600 "$S"
  echo "Claude Code: hint headers, project and client headers added to $S"
}

j_portal() {
  # The portal, for your browser through the tunnel to this host (README section 4).
  sudo systemctl enable --now ltm-forward@8100
  wait_for http://127.0.0.1:8100/portal/api/config 60
}

step j00-openwebui   j_openwebui
step j01-claude-code j_claude_code
step j02-portal      j_portal

cat <<EOF

==============================================================================
 Phase 1, VM-2 done. Delete the handoff file now: rm $IN

 One step in Open WebUI's UI (optional, its connections live in its database):
   Admin Settings > Connections > the LiteLLM connection > Headers:
     {"x-memmachine-client": "openwebui"}
 Without it the hook still recognises Open WebUI by its signed token.

 Undo: $ENV_DIR/openwebui.env.before-memmachine, ~/.claude/settings.json.before-memmachine
 Then, on VM-1: bash deploy/aws/1-integrate/verify.sh
==============================================================================
EOF
