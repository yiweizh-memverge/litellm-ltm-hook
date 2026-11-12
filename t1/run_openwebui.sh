#!/usr/bin/env bash
# Run Open WebUI for T1 from its venv, one data directory per version (an older
# release cannot open a newer one's database).
#
#   t1/run_openwebui.sh                 # version from compose.env (OPENWEBUI_VERSION)
#   OPENWEBUI_VERSION=v0.11.3 t1/run_openwebui.sh
#
# Install a version first:
#   uv venv $T1_ENV/owui-v0.11.3 --python 3.12
#   VIRTUAL_ENV=$T1_ENV/owui-v0.11.3 uv pip install 'open-webui==0.11.3'
set -euo pipefail
T1_ENV=${T1_ENV:-$HOME/t1-tokenpath-env}
ASKED=${OPENWEBUI_VERSION:-}
set -a; . "$T1_ENV/compose.env"; . "$T1_ENV/openwebui.env"; set +a
VER=${ASKED:-$OPENWEBUI_VERSION}
export DATA_DIR=$T1_ENV/owui-data-$VER
mkdir -p "$DATA_DIR"
exec "$T1_ENV/owui-$VER/bin/open-webui" serve --host 127.0.0.1 --port "$PORT"
