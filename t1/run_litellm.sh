#!/usr/bin/env bash
# Run LiteLLM 1.98.0 for T1; callbacks load from t1/. Use t1/svc.sh to start it.
#   LITELLM_CONFIG=litellm-config-forward.yaml   another config in t1/
#   LITELLM_EXTRA_ARGS=--detailed_debug           extra flags
set -euo pipefail
T1_ENV=${T1_ENV:-$HOME/t1-tokenpath-env}
cd "$(dirname "$0")"
set -a; . "$T1_ENV/litellm.env"; set +a
export T1_PROBE_LOG=${T1_PROBE_LOG:-$T1_ENV/logs/probe.jsonl}
export T1_HOOK_LOG=${T1_HOOK_LOG:-$T1_ENV/logs/hook.jsonl}
export T1_GATEWAY_URL=${T1_GATEWAY_URL:-http://127.0.0.1:18100}
export PATH=$T1_ENV/litellm-venv/bin:$PATH
# shellcheck disable=SC2086
exec litellm --config "${LITELLM_CONFIG:-litellm-config.yaml}" --host 127.0.0.1 --port "$LITELLM_PORT" ${LITELLM_EXTRA_ARGS:-}
