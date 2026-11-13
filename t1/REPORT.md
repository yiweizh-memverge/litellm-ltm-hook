# T1 report: approach A's user-token path

**Date:** 2026-11-12 · **Branch:** `validate/t1-token-path` · **Design:** MemVerge/MemMachine-Platform#1561 at `4e15ccc` (re-read against `520b19c`, below)

**Result:** with the versions below and T1's stand-in gateway, hook and model provider, the basic
personal-memory path with the user's token **forwarded as is** works on SK hynix's LiteLLM
version. Each Open WebUI user's own Keycloak access token reaches the hook through LiteLLM, the
gateway verifies it itself, memory round-trips per person and stays isolated, and the token reached
no model provider, log or database we checked. Not verified: a real Open WebUI streaming and
tool-server turn, several LiteLLM workers, team permissions, the token-exchange path (the PR's
recommended mode since `63964d6`), and the real platform behind the gateway.

**PR changes since `4e15ccc`:** the PR now recommends exchanging the token at the provider, with
forwarding as the fallback, so T1 tested the fallback; and an unresolved `{{MODEL_ID}}` now uses
personal memory instead of none (finding 1). T1's hook still applies the old rule.

## Versions

| Component | Version |
|---|---|
| LiteLLM | 1.98.0 (SK hynix's) |
| Open WebUI | 0.12.5, and 0.11.3 |
| Keycloak | 26.7.5, realm `corp`, default 300 s access tokens (see note) |
| Gateway, hook, model provider | T1 stand-ins: `t1/gateway.py`, `t1/t1_token_hook.py`, `t1/fake_provider.py` |

Open WebUI's connection: `system_oauth`, LiteLLM's key in `x-litellm-api-key`, and per-request
headers `{{CHAT_ID}}`, `{{MESSAGE_ID}}`, `{{USER_MESSAGE_ID}}`, `{{TASK}}`, `{{MODEL_ID}}`.

Note: after A7, Open WebUI's client was left at 900 s (`set_owui_lifespan(None)` dropped the
attribute from the update, which Keycloak ignores; fixed 2026-11-12), so the `core` runs used
900 s Open WebUI tokens. No `core` check depends on it: A6 uses tokens minted by `t1-tester`.

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

1. **`{{MODEL_ID}}` needs Open WebUI 0.12.2 (confirmed). Taken into the PR at `520b19c`.** On
   0.11.3 the placeholder is sent unresolved, and under `4e15ccc`'s rule ("an unresolved placeholder
   → no memory"), which T1's hook implements, every request on the connection got no memory,
   personal included. Without that header, 0.11.3 passes every check. `520b19c` now asks for 0.12.2
   for team projects only and treats an unresolved model placeholder as personal memory; T1's hook
   has not been changed to that rule or re-run.
2. **At Keycloak's default 300 s access token, Open WebUI refreshes on every request.** It
   refreshes when 5 minutes or less are left, which is always: a browser prompt cost ~5 Keycloak
   refreshes (the chat plus three background tasks plus one), a scripted prompt 2. At 900 s:
   none within a short run; a run across several token lifetimes would show the steady state. Matches #1561's "Open WebUI's token lifetime"; it multiplies Keycloak load ~5× per prompt
   at SK's scale and makes the token differ on every request of a turn (A6). SK should give
   Open WebUI's client a longer access-token lifetime (second-round question).
3. **`forward_llm_provider_auth_headers` on did not send the user's token to our provider.**
   #1561 says it would, still at `520b19c`. Here the model is `openai/…` with its own `api_key` in the config, and the
   provider still got that key. It may differ for other providers or a model without a configured
   key; not run. The hook's guard (memory off when the setting is on) works either way.

## Also observed

- Open WebUI 0.11.3 sends the user's token only with the browser's cookies; 0.12.x falls back
  to the user's latest session, so API keys and automations reach memory as that person (A11),
  as #1561's "What can go wrong" says.
- LiteLLM's spend logs keep request headers, minus `authorization` and `x-litellm-api-key`: the
  `x-openwebui-*` IDs are kept.
- A new workspace model is chat-able only after Open WebUI's model list refreshes (`/api/models`).

## T1b: a real streaming turn and a real tool server (2026-11-13)

`$PY t1/t1b_stream_tool.py` (the Open WebUI venv's Python, for socket.io); raw results in
`$T1_ENV/logs/results-v0.12.5-t1b.json`. Open WebUI 0.12.5 runs tools (native function calling)
only for a chat page's session, so the script signs alice in and connects to Open WebUI's socket.io
as the page does, registers `t1/tool_server.py` (one OpenAPI tool, `lookup_fact`) as a tool server,
and sends two prompts with streaming on. The fake provider streams text in 8-character pieces.

| Check | Plain streaming turn | Tool turn |
|---|---|---|
| The page got the answer streamed; its deltas add up to the final text | PASS (66 deltas) | PASS (66 deltas; output: function call, its result, message) |
| Every model request streamed, all of one turn | PASS (1 request) | PASS (2 requests; the second ends with the tool result) |
| The tool server called once, by Open WebUI's server | — | PASS |
| The hook searched once and stored once (store accepted) | PASS | PASS (`search`, `tool-round`, `token-changed`, `reuse`, `store`) |
| The stored memory is the prompt and the answer the page received, exactly | PASS | PASS |

In the tool turn Open WebUI refreshed the user's token between the two model requests (300 s
tokens); the store used the latest token and was accepted, A6 in a real flow. Open WebUI offered the
model 27–28 tools, its built-in ones besides `lookup_fact`. The tool server got no user token
(`auth_type` none). LiteLLM does not pass Open WebUI's `x-openwebui-*` headers on to the provider.

Not covered: cancel, regenerate, a broken stream, provider errors and retries, several tool rounds,
a tool server on `system_oauth` (implementation acceptance, not the gate).

## T1c: a new token mid-turn is re-checked before it gets memory (2026-11-13)

Before T1c the stand-in hook, on a new token mid-turn, kept the turn's memory and added it back
(`token-changed`, `reuse`): it showed a new token could store, not that it was authorized before
receiving the cached memory. #1561's hook re-checks the turn's binding with the new token first.
The stand-in now follows #1561's table: it keeps each token's outcome by hash; a new token is
re-checked at the gateway (`POST /api/v2/memories/binding/check`, the stand-in's name: the same
token checks and binding rule as a store, from the account table alone, no search); accepted, the
result is added back; refused or failed, nothing with that token for the rest of the turn. A turn
with no binding searches again with its prompt, which gives the binding and adds nothing.

`$PY t1/t1c_recheck.py`; raw results in `$T1_ENV/logs/results-v0.12.5-t1c.json`. One turn per
case, sent to LiteLLM as Open WebUI sends it, a tool round in between:

| Case | Re-check | Memory added (per request) | Store |
|---|---|---|---|
| R1 same person, new token | 200 | yes, yes | 200 |
| R2 another person's token, two more requests | 409 `turn-outdated`, once | yes, no, no | 409 |
| R3 the person's memory erased mid-turn | 409 `turn-outdated` | yes, no | 409 |
| R4 the person disabled mid-turn | 403 `account-disabled` | yes, no | 403 |
| R5 same token | none | yes, yes | 200 |

In every case the gateway served one search per turn, the first; the re-check was not a search.
T1's core checks and T1b still pass; T1b's tool turn now reads `search`, `tool-round`,
`token-changed`, `recheck`, `store`: Open WebUI's refresh between the two model requests is
re-checked, accepted, then stored. The re-check's failure path (a timeout or `unavailable`) was not
run.

## T6 gate check: Keycloak hangs or is down (2026-11-12)

`$PY t1/t6_hang.py`; raw results in `$T1_ENV/logs/results-t6.json`. Open WebUI 0.12.5, Keycloak
paused with `docker pause` (accepts connections, never answers), then stopped (connection refused).

| Case | Chat answered after | Memory |
|---|---|---|
| Keycloak up (baseline) | 0.1 s | yes |
| Hang, `system_oauth`, one session, 300 s token | **333 s** | no |
| Hang, same person's second chat 5 s later | **629 s** | no |
| Hang, person with two Open WebUI sessions | **634 s** | no |
| Hang, a connection with **no user token** (Open WebUI without approach A) | **333 s** | — |
| Hang, 900 s token, refresh not yet due | 0.1 s | yes |
| Keycloak stopped (connection refused) | 0.1 s | no |
| After Keycloak recovers, for everyone whose refresh failed | 0.1 s | **no, until they sign in to Open WebUI again** |

Why, in Open WebUI's code (0.12.5; 0.11.3 the same):

- The chat middleware retrieves the person's SSO token (`'__oauth_token__': await
  get_system_oauth_token(...)` in `utils/middleware.py`), **whatever the connection's auth type**,
  at several points: preparing the request (`process_chat_payload`), handling a streaming response
  (`streaming_chat_response_handler`), and, only with filter functions, resuming tool calls
  (`resume_tool_calls`). Each lookup refreshes only when 5 minutes or less are left
  (`get_oauth_token`), so these are lookups, not a fixed number of refreshes per chat; refresh is
  on demand, never on a timer.
- The refresh (`_perform_token_refresh` in `utils/oauth.py`) sets no timeout, so aiohttp's default
  applies: `ClientTimeout(total=300)`. A refresh that fails, by timeout or refusal, **deletes the
  session**, so the person's token is gone until they sign in again.
- A second chat of the same session waits on the per-session refresh lock, then refreshes again
  with the deleted session's token: two timeouts. A person with several sessions (browsers,
  devices) falls back to the most recent other one: two timeouts too.

So:

1. **The hang is Open WebUI's, not approach A's.** A chat on a connection that sends no user token
   stalled the same 333 s. Any Open WebUI signed in through Keycloak is exposed today, approach B
   included; approach A adds nothing to how long a chat waits.
2. **What approach A adds is silent memory loss.** After any failed refresh, a hang or a short
   outage, the person keeps chatting without memory until they sign in to Open WebUI again, with
   nothing telling them. #1561 names this ("Open WebUI dropped the person's corporate tokens after
   a failed refresh"); this measures it.
3. **A longer access-token lifetime shrinks the exposure** (a 900 s token with more than 5 minutes
   left was untouched), at the cost of the revocation window (T5). A real fix is in Open WebUI: a
   short refresh timeout, and not deleting the session on a timeout or connection error.

Not run: the ~30 s beyond the 300 s timeout is unexplained; Open WebUI with several workers or
Redis; the browser's own behaviour while waiting (the chat UI, not the API).

## Not covered by T1

Token exchange (T4), offboarding (T5), load (T7), several LiteLLM workers, cancelled or
regenerated replies, a stale binding after an erase, disable or team change, team projects, and
MemMachine core behind the gateway. (Streaming and a real tool server: T1b; Keycloak outages: T6.)

## Test-only setup

A Keycloak client `t1-tester` (password and client-credentials grants, 10 s tokens) mints tokens
for A3–A9 and is allowed by the T1 gateway. Open WebUI's `WEBUI_URL` is `http://127.0.0.1:18088`
because the browser reached it through an IDE tunnel on that port (`t1/port_alias.py`).
