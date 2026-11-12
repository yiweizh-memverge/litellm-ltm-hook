# T1 report: approach A's user-token path

**Date:** 2026-11-12 · **Branch:** `validate/t1-token-path` · **Design:** MemVerge/MemMachine-Platform#1561

**Result:** the path works on SK hynix's LiteLLM version. Each Open WebUI user's own Keycloak
access token reaches the hook through LiteLLM, the gateway verifies it itself, memory
round-trips per person and stays isolated, and the token reached no model provider, log or
database we checked. Three findings need action (below).

## Versions

| Component | Version |
|---|---|
| LiteLLM | 1.98.0 (SK hynix's) |
| Open WebUI | 0.12.5, and 0.11.3 |
| Keycloak | 26.7.5, realm `corp`, default 300 s access tokens |
| Gateway, hook, model provider | T1 stand-ins: `t1/gateway.py`, `t1/t1_token_hook.py`, `t1/fake_provider.py` |

Open WebUI's connection: `system_oauth`, LiteLLM's key in `x-litellm-api-key`, and per-request
headers `{{CHAT_ID}}`, `{{MESSAGE_ID}}`, `{{USER_MESSAGE_ID}}`, `{{TASK}}`, `{{MODEL_ID}}`.

## Results

`$PY t1/run_checks.py core|refresh|forward|debug|leaks`; raw results in `$T1_ENV/logs/results-*.json`.

| ID | Check | 0.12.5 | 0.11.3 |
|---|---|---|---|
| A1 | Memory round trip through Open WebUI: one search and one store per turn | PASS | PASS without the `{{MODEL_ID}}` header; **no memory at all with it** (finding 1) |
| A2 | Isolation: bob gets none of alice's memory; separate accounts | PASS | PASS |
| A3 | Gateway refuses: no token, not a JWT, altered signature, HS256 forgery, another realm, ID token, a client not named for the API, a client-credentials token, an expired token | PASS (9/9) | PASS (9/9) |
| A3 | Alice's token from another allowed client maps to the same account (same `sub`) | PASS | PASS |
| A3 | A refused token through LiteLLM: no memory, the chat still answers | PASS | PASS |
| A4 | No memory call for an Open WebUI task (title, tags, follow-ups), an unresolved placeholder, or no user token | PASS | PASS |
| A5 | Turn with a tool round: one search, one store | PASS | PASS |
| A6 | Token changes mid-turn (Open WebUI refreshes per request); the store uses the latest and is accepted | PASS | PASS |
| A7 | Open WebUI refreshes the token on every request at 300 s; none at 900 s | INFO (finding 2) | — |
| A8 | No token in the provider's requests, LiteLLM's stdout and `--detailed_debug` log, LiteLLM's database (`pg_dump`, spend logs included), the gateway's, hook's or probe's logs | PASS | — |
| A9 | `forward_llm_provider_auth_headers` on: the hook turns memory off | PASS | — |
| A9 | … and does the token reach the provider? | INFO (finding 3) | — |
| A10 | LiteLLM refuses the user's token alone (401); spend is booked to Open WebUI's key | PASS | PASS |
| A11 | Open WebUI API call without the browser's cookies | token sent, memory used | no token, no memory |
| A12 | Workspace model: LiteLLM gets the base model; `x-openwebui-model-id` names the workspace model | `t1-team-model` | literal `{{MODEL_ID}}` |

Browser session (alice, two chats, 2026-11-11): message IDs filled in on chat requests; each
prompt also sent `title_generation`, `follow_up_generation` and `tags_generation` requests with
`x-openwebui-task` set, an empty message ID and the task model, which the hook left alone. **Every
one of those requests also carried the user's token.**

## Findings

1. **`{{MODEL_ID}}` needs Open WebUI 0.12.2 (confirmed).** On 0.11.3 the placeholder is sent
   unresolved, and under #1561's rule ("an unresolved placeholder → no memory") every request on
   the connection gets no memory, personal included. Without that header, 0.11.3 passes every
   check. So: 0.10.0 for personal memory, 0.12.2 for team projects by workspace model, and the
   `{{MODEL_ID}}` header only on connections that need it. (A comment for #1561 is drafted.)
2. **At Keycloak's default 300 s access token, Open WebUI refreshes on every request.** It
   refreshes when 5 minutes or less are left, which is always: a browser prompt cost ~5 Keycloak
   refreshes (the chat plus three background tasks plus one), a scripted prompt 2. At 900 s:
   none. Matches #1561's "Open WebUI's token lifetime"; it multiplies Keycloak load ~5× per prompt
   at SK's scale and makes the token differ on every request of a turn (A6). SK should give
   Open WebUI's client a longer access-token lifetime (second-round question).
3. **`forward_llm_provider_auth_headers` on did not send the user's token to our provider.**
   #1561 says it would. Here the model is `openai/…` with its own `api_key` in the config, and the
   provider still got that key. It may differ for other providers or a model without a configured
   key; not run. The hook's guard (memory off when the setting is on) works either way.

## Also observed

- Open WebUI 0.11.3 sends the user's token only with the browser's cookies; 0.12.x falls back
  to the user's latest session, so API keys and automations reach memory as that person (A11),
  as #1561's "What can go wrong" says.
- LiteLLM's spend logs keep request headers, minus `authorization` and `x-litellm-api-key`: the
  `x-openwebui-*` IDs are kept.
- A new workspace model is chat-able only after Open WebUI's model list refreshes (`/api/models`).

## Not covered by T1

Offboarding (T5), Keycloak outages and hangs (T6), load (T7), a real tool server in Open WebUI
(A5 used LiteLLM directly with Open WebUI's headers), streaming responses through the hook, and
MemMachine core behind the gateway.

## Test-only setup

A Keycloak client `t1-tester` (password and client-credentials grants, 10 s tokens) mints tokens
for A3–A9 and is allowed by the T1 gateway. Open WebUI's `WEBUI_URL` is `http://127.0.0.1:18088`
because the browser reached it through an IDE tunnel on that port (`t1/port_alias.py`).
