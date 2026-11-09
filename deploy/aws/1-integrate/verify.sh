#!/usr/bin/env bash
# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0
#
# After phase 1 on both VMs, on VM-1: the repo's end-to-end checks
# (tools/platform_e2e.py) against this two-VM layout. Open WebUI is VM-2's; the
# checks sign people in to it through Keycloak here, as a browser would.
#
#   bash deploy/aws/1-integrate/verify.sh
#
# The checks were written for the one-host stack of SETUP.md; a failure here can
# be a layout difference rather than a fault. Read the failing check's name.
# shellcheck source=../lib.sh
. "$(dirname "$0")/../lib.sh"

prepare
[[ -e $STATE/i03-upgrade.done ]] || die "phase 1 has not finished on this host (1-integrate/server.sh)"
CLIENT_IP=$(cat "$STATE/client_ip")
# shellcheck disable=SC1091
. "$STATE/secrets.env"
cd "$REPO"
LITELLM_URL=http://127.0.0.1:4011 OWUI_URL=http://$CLIENT_IP:8087 PLATFORM_URL=http://127.0.0.1:8100 \
PLATFORM_DB=$ENV_DIR/platform/registry.sqlite KEYS_FILE=$ENV_DIR/keys.env \
ACCOUNTS_FILE=$ENV_DIR/accounts.txt OWUI_JWT_SECRET="$(cat "$STATE/owui-jwt-secret")" \
PLATFORM_SERVICE_CREDENTIAL="$(cat "$STATE/service-credential")" LITELLM_MASTER_KEY="$LITELLM_MASTER_KEY" \
  uv run --group platform python3 tools/platform_e2e.py
