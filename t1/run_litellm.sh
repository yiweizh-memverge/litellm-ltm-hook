#!/usr/bin/env bash
# Run LiteLLM 1.98.0 for T1 with t1/litellm-config.yaml; callbacks load from t1/.
#   nohup t1/run_litellm.sh > $T1_ENV/logs/litellm.out 2>&1 &
set -euo pipefail
T1_ENV=${T1_ENV:-$HOME/t1-tokenpath-env}
cd "$(dirname "$0")"
set -a; . "$T1_ENV/litellm.env"; set +a
export T1_PROBE_LOG=${T1_PROBE_LOG:-$T1_ENV/logs/probe.jsonl}
export PATH=$T1_ENV/litellm-venv/bin:$PATH
exec litellm --config litellm-config.yaml --host 127.0.0.1 --port "$LITELLM_PORT"
