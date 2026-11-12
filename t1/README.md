# T1: the user-token path for approach A (validation only)

Checks the Open WebUI → LiteLLM → hook path that MemVerge/MemMachine-Platform#1561
(approach A) relies on: Open WebUI sends each user's Keycloak access token
(`system_oauth`), LiteLLM authenticates Open WebUI's shared key from
`x-litellm-api-key`, and the hook forwards the user's token to a gateway that
verifies it itself. The token must reach nothing else: not the model provider,
not LiteLLM's logs or database.

This branch is for validation only. Nothing here changes `memmachine_hook.py` or
the approach-B mock platform; everything lives in `t1/`. The test plan is the
Confluence page "PR #1561 (Approach A) — internal feasibility test plan for SK
hynix", test T1.

## Pieces

| Piece | Where | Port |
|---|---|---|
| Keycloak 26.7.5, realm `corp` | docker (`t1/docker-compose.yml`) | 18180 |
| Postgres 16 for LiteLLM's keys | docker | 15432 |
| LiteLLM 1.98.0 (SK hynix's version) | venv `$T1_ENV/litellm-venv` | 18011 |
| Open WebUI 0.12.5 (and 0.11.3) | venv `$T1_ENV/owui-<version>` | 18087 |
| Fake model provider, records everything it is sent | `t1/fake_provider.py` | 18200 |
| Mock gateway, verifies Keycloak access tokens | `t1/gateway.py` (step 2) | 18100 |
| Hook under test | `t1/t1_token_hook.py` (step 2) | in LiteLLM |

Runtime files, secrets included, live in `$T1_ENV` (default `~/t1-tokenpath-env`),
never in the repo.

## Setup

```bash
cd <this repo>
t1/init_env.sh                                   # secrets and env files in $T1_ENV
docker compose -f t1/docker-compose.yml --env-file ~/t1-tokenpath-env/compose.env up -d

# LiteLLM 1.98.0 (+ deps for the fake provider and gateway)
cd ~/t1-tokenpath-env
uv venv litellm-venv --python 3.12
VIRTUAL_ENV=litellm-venv uv pip install 'litellm[proxy]==1.98.0' prisma fastapi uvicorn 'pyjwt[crypto]' httpx
S=$(litellm-venv/bin/python -c "import litellm,os; print(os.path.join(os.path.dirname(litellm.__file__),'proxy','schema.prisma'))")
PATH=$PWD/litellm-venv/bin:$PATH litellm-venv/bin/prisma generate --schema "$S"

# Open WebUI
uv venv owui-v0.12.5 --python 3.12
VIRTUAL_ENV=owui-v0.12.5 uv pip install 'open-webui==0.12.5'
cd -

# Fake provider and LiteLLM
FAKE_PROVIDER_LOG=~/t1-tokenpath-env/logs/provider.jsonl \
  nohup ~/t1-tokenpath-env/litellm-venv/bin/python t1/fake_provider.py > ~/t1-tokenpath-env/logs/fake_provider.out 2>&1 &
(cd t1 && set -a && . ~/t1-tokenpath-env/litellm.env && set +a && \
  PATH=$HOME/t1-tokenpath-env/litellm-venv/bin:$PATH nohup litellm --config litellm-config.yaml \
  --host 127.0.0.1 --port $LITELLM_PORT > ~/t1-tokenpath-env/logs/litellm.out 2>&1 &)

t1/finish_env.sh                                 # realm corp, Open WebUI's LiteLLM key
nohup t1/run_openwebui.sh > ~/t1-tokenpath-env/logs/openwebui.out 2>&1 &
```

Open WebUI reads `OPENAI_API_CONFIGS` only on its first start with an empty data
directory; to change the connection later, use Admin Settings → Connections, or
delete `$T1_ENV/owui-data-<version>`.

Test users are in `$T1_ENV/accounts.txt` (`email password`). From a laptop:
`ssh -L 18087:127.0.0.1:18087 -L 18180:127.0.0.1:18180 <this host>`, then open
http://127.0.0.1:18087 and sign in with "Corp SSO".

## Notes

- Open WebUI runs from a venv, not its container image: the image does not fit
  on this host's root disk.
- LiteLLM needs `prisma generate` once, or it fails at start with "Unable to find
  Prisma binaries".
