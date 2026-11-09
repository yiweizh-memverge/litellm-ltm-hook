#!/usr/bin/env bash
# Copyright 2026 litellm-ltm-hook contributors
# SPDX-License-Identifier: Apache-2.0
#
# VM-2: start Claude Code as one person, in a new empty folder (a new session).
#
#   bash deploy/aws/claude-as.sh bob                 # asks for Bob's key the first time
#   bash deploy/aws/claude-as.sh bob -p "What is my badge PIN hint?"   # one question
#   bash deploy/aws/claude-as.sh bob --key           # asks for the key again
#   bash deploy/aws/claude-as.sh bob --setup         # store the key only, start nothing
#
# Keys come from VM-1 (bash deploy/aws/show-keys.sh <name>), typed or piped in;
# they are never shown. Each person, Alice too, gets ~/.claude-<name>/settings.json,
# rebuilt on every start from ~/.claude/settings.json (gateway, model, and the
# MemMachine headers phase 1 adds there) with only their own key swapped in. So
# people set up during phase 0 pick up phase 1's headers by themselves. Whatever
# key ~/.claude itself holds is never used. Claude Code's own auto memory, its
# away recap and its prompt suggestions are turned off, and ~/.claude-<name>/CLAUDE.md
# tells the model not to keep notes in files: anything remembered across
# sessions comes from MemMachine.
set -euo pipefail
export PATH=$HOME/.local/bin:$PATH
WHO=${1:?usage: claude-as.sh <name> [--key|--setup] [claude args...]}
shift
WHO=$(tr -cd 'a-z0-9' <<<"${WHO,,}")
ASK=0 RUN=1
case ${1:-} in
  --key)   ASK=1; shift ;;
  --setup) ASK=1; RUN=0; shift ;;
esac

BASE=~/.claude/settings.json
DIR=~/.claude-$WHO
OUT=$DIR/settings.json
[[ -s $BASE ]] || { echo "no $BASE: run phase 0 (0-baseline/client.sh) on this host first" >&2; exit 1; }

KEY=
if [[ $ASK == 0 && -s $OUT ]]; then
  KEY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["env"]["ANTHROPIC_AUTH_TOKEN"])' "$OUT")
else
  [[ -t 0 ]] && printf '%s key (from VM-1: bash deploy/aws/show-keys.sh %s): ' "$WHO" "$WHO" >&2
  read -rs KEY || true
  [[ -t 0 ]] && echo >&2
fi
[[ $KEY == sk-* ]] || { echo "$WHO: that is not a LiteLLM key (sk-...)" >&2; exit 1; }

mkdir -p "$DIR"; chmod 700 "$DIR"
CC_KEY=$KEY python3 - "$BASE" "$OUT" <<'PY'
import json, os, sys
base, out = sys.argv[1:]
s = json.load(open(base))
s["env"]["ANTHROPIC_AUTH_TOKEN"] = os.environ["CC_KEY"]
# Claude Code's own auto memory off: what it remembers must come from MemMachine.
s["autoMemoryEnabled"] = False
s["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
# No requests the person did not type: the recap after being away and the
# next-prompt suggestions are model calls too, and the hook would store them.
s["env"]["CLAUDE_CODE_ENABLE_AWAY_SUMMARY"] = "0"
s["env"]["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] = "0"
fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(s, f, indent=2)
os.chmod(out, 0o600)
PY
# With auto memory off the model may still write a CLAUDE.md to "remember".
cat >"$DIR/CLAUDE.md" <<'MD'
# Memory

Long-term memory is provided by the company's memory platform: what the user
tells you is kept and recalled across sessions automatically. Do not create or
edit files (CLAUDE.md, notes, memory files) to remember facts; just acknowledge
them.
MD
unset KEY
if [[ $RUN == 0 ]]; then
  echo "$WHO: key stored in $OUT" >&2
  exit 0
fi

# A new empty folder per session: answers can only come from MemMachine, not files.
S=~/sessions/$WHO-$(date +%H%M%S)
mkdir -p "$S" && cd "$S"
echo "Claude Code as $WHO, in $S" >&2
CLAUDE_CONFIG_DIR=$DIR exec claude "$@"
