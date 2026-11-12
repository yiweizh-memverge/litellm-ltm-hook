#!/usr/bin/env bash
# Start, stop or restart T1's host-side services, tracked by pid files in $T1_ENV/run.
#
#   t1/svc.sh start|stop|restart provider|gateway|litellm|openwebui|alias|all
#   t1/svc.sh status
#
# litellm reads LITELLM_CONFIG (default t1/litellm-config.yaml) and LITELLM_EXTRA_ARGS
# (e.g. --detailed_debug); openwebui reads OPENWEBUI_VERSION (default from compose.env);
# gateway reads T1_ALLOWED_CLIENTS.
set -euo pipefail
T1_ENV=${T1_ENV:-$HOME/t1-tokenpath-env}
T1=$(cd "$(dirname "$0")" && pwd)
PY=$T1_ENV/litellm-venv/bin/python
RUN=$T1_ENV/run; LOGS=$T1_ENV/logs
mkdir -p "$RUN" "$LOGS"

port_of() { case $1 in provider) echo 18200;; gateway) echo 18100;; litellm) echo 18011;; openwebui) echo 18087;; alias) echo 18088;; esac; }
health_of() {
  case $1 in
    provider) echo http://127.0.0.1:18200/v1/models;;
    gateway) echo http://127.0.0.1:18100/t1/state;;
    litellm) echo http://127.0.0.1:18011/health/liveliness;;
    openwebui|alias) echo http://127.0.0.1:$(port_of "$1")/health;;
  esac
}

start_one() {
  local s=$1
  if [ -f "$RUN/$s.pid" ] && kill -0 "$(cat "$RUN/$s.pid")" 2>/dev/null; then echo "$s: already running"; return; fi
  case $s in
    provider) FAKE_PROVIDER_LOG=$LOGS/provider.jsonl setsid "$PY" "$T1/fake_provider.py" >"$LOGS/fake_provider.out" 2>&1 & ;;
    gateway)  T1_GATEWAY_LOG=$LOGS/gateway.jsonl T1_ALLOWED_CLIENTS=${T1_ALLOWED_CLIENTS:-open-webui,mm-auth,t1-tester} \
                setsid "$PY" "$T1/gateway.py" >"$LOGS/gateway.out" 2>&1 & ;;
    litellm)  setsid "$T1/run_litellm.sh" >"$LOGS/litellm.out" 2>&1 & ;;
    openwebui) setsid "$T1/run_openwebui.sh" >"$LOGS/openwebui.out" 2>&1 & ;;
    alias)    setsid "$PY" "$T1/port_alias.py" 18088 18087 >"$LOGS/port_alias.out" 2>&1 & ;;
  esac
  echo $! >"$RUN/$s.pid"
  local url; url=$(health_of "$s")
  for _ in $(seq 1 100); do curl -sf "$url" >/dev/null 2>&1 && { echo "$s: up"; return; }; sleep 3; done
  echo "$s: not healthy after 300 s (see $LOGS)"; return 1
}

stop_one() {
  local s=$1 pid
  if [ -f "$RUN/$s.pid" ]; then
    pid=$(cat "$RUN/$s.pid")
    kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
    rm -f "$RUN/$s.pid"
  fi
  # Anything else still on the port (started before pid files existed).
  local p; p=$(port_of "$s")
  for pid in $(ss -ltnp 2>/dev/null | awk -v p=":$p" '$4 ~ p"$"' | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u); do
    kill "$pid" 2>/dev/null || true
  done
  for _ in $(seq 1 20); do ss -ltn | awk '{print $4}' | grep -q ":$p\$" || { echo "$s: stopped"; return; }; sleep 1; done
  echo "$s: port $p still in use"
}

ALL="provider gateway litellm openwebui alias"
cmd=${1:-status}; target=${2:-all}
[ "$target" = all ] && targets=$ALL || targets=$target
case $cmd in
  start)   for s in $targets; do start_one "$s"; done ;;
  stop)    for s in $targets; do stop_one "$s"; done ;;
  restart) for s in $targets; do stop_one "$s"; start_one "$s"; done ;;
  status)  for s in $ALL; do curl -sf "$(health_of "$s")" >/dev/null 2>&1 && echo "$s: up" || echo "$s: down"; done ;;
  *) echo "usage: $0 start|stop|restart|status [service|all]"; exit 2 ;;
esac
