#!/usr/bin/env bash
# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0
#
# VM-1: print the people's LiteLLM keys from keys.env, to use them in Claude Code
# on VM-2 (claude-as.sh). They are secrets: print them on your own screen only.
#
#   bash deploy/aws/show-keys.sh          # every person: name, key
#   bash deploy/aws/show-keys.sh bob      # just Bob's key, nothing else (for pipes)
set -euo pipefail
KF=/opt/ltm-platform-env/keys.env
[[ -r $KF ]] || { echo "no $KF: has phase 0 run on this host?" >&2; exit 1; }

if [[ $# -eq 0 ]]; then
  grep -E '^[A-Z0-9]+_VIRTUAL_KEY=' "$KF" | while IFS='=' read -r var key; do
    printf '%-8s %s\n' "$(tr A-Z a-z <<<"${var%_VIRTUAL_KEY}")" "$key"
  done
  echo "(Open WebUI's shared key, OPENWEBUI_APP_KEY, is not a person's: not listed)" >&2
else
  var=$(tr -cd 'A-Za-z0-9' <<<"$1" | tr a-z A-Z)_VIRTUAL_KEY
  key=$(grep "^$var=" "$KF" | cut -d= -f2-) || true
  [[ -n $key ]] || { echo "no $var in $KF" >&2; exit 1; }
  printf '%s\n' "$key"
fi
