#!/usr/bin/env bash
# Start/stop the real e2e run's host processes: the MiniLM embedder and the real memmachine-gateway.
#   e2e/svc.sh start|stop|restart embed|gateway|all
# Postgres, Qdrant and MemMachine core run from e2e/docker-compose.yml. Run from the repo root.
#
# The gateway is the platform's own (MemVerge/MemMachine-Platform cmd/memmachine-gateway, built to
# $T1_ENV/real/bin), pointed at the T1 Keycloak's corp realm instead of the platform's memmachine
# realm. It listens on every interface (it hard-codes ":port"); it admits only that realm's tokens.
set -euo pipefail
T1_ENV=${T1_ENV:-$HOME/t1-tokenpath-env}
LOGS=$T1_ENV/logs
KC=http://127.0.0.1:18180/realms/corp
mkdir -p "$LOGS"

port_of() { case $1 in embed) echo 18210;; gateway) echo 18110;; esac; }

start() {
  case $1 in
    embed)   E2E_EMBED_LOG=$LOGS/embed.jsonl setsid "$T1_ENV/embed-venv/bin/python" e2e/embed_server.py \
               >"$LOGS/embed.out" 2>&1 & ;;
    gateway) set -a; . "$T1_ENV/real/e2e.env"; set +a
             # E2E_AUDIENCE set: the build with the e2e test patch (an --audience flag), accepting that
             # audience instead of main's hard-coded "account"
             bin=memmachine-gateway; aud=()
             if [ -n "${E2E_AUDIENCE:-}" ]; then bin=memmachine-gateway-aud; aud=(--audience "$E2E_AUDIENCE"); fi
             MMP_POSTGRES_PASSWORD=$POSTGRES_PASSWORD setsid "$T1_ENV/real/bin/$bin" "${aud[@]}" \
               --port 18110 --gauge-port 18119 --upstream-url http://127.0.0.1:18081 \
               --jwks-url "$KC/protocol/openid-connect/certs" --issuer-url "$KC" \
               --postgres-host 127.0.0.1 --postgres-port 15433 --postgres-user memmachine \
               --postgres-database memmachine_backend --semantic-memory-enabled=false \
               >>"$LOGS/realgw.jsonl" 2>>"$LOGS/realgw.err" & ;;
  esac
  for _ in $(seq 60); do
    if curl -s -m1 -o /dev/null "http://127.0.0.1:$(port_of "$1")/health"; then echo "$1: up"; return; fi
    sleep 1
  done
  echo "$1: did not come up" >&2; return 1
}

stop() {
  local pids; pids=$(ss -ltnpH "sport = :$(port_of "$1")" | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u)
  [ -n "$pids" ] && kill $pids 2>/dev/null || true
  echo "$1: stopped"
}

cmd=$1; shift
targets=${*:-all}; [ "$targets" = all ] && targets="embed gateway"
for t in $targets; do
  case $cmd in start) start "$t";; stop) stop "$t";; restart) stop "$t"; sleep 1; start "$t";; esac
done
