# End to end with the real platform: Open WebUI and Claude Code to MemMachine

**Date:** 2026-11-13 · **Branch:** `validate/t1-token-path` · **Design:** MemVerge/MemMachine-Platform#1561 at `086cc62`

**Result:** both clients store and recall memory end to end through the real memmachine-gateway and
MemMachine core, with a real embedding model (DeepInfra's `BAAI/bge-m3`). Every check passed: 16 PASS
and 2 INFO in `e2e/e2e_check.py`. That covers:

- Open WebUI through LiteLLM's hook, with the employee's token forwarded or exchanged.
- Claude Code with `mm-auth`, calling the gateway directly or through LiteLLM's pass-through route
  (Options A and B).
- One person across both clients.
- Isolation between people.
- A real chat model (DeepInfra's Llama 3.3 70B) answering from memory, in Open WebUI and Claude
  Code, including a fact told in Korean and asked about in English.

The gateway as it is on main accepts only forwarded tokens. #1561's recommended exchange mode
needs the gateway to accept the platform audience. A one-flag test patch shows that change is
enough (finding 1).

## What was real

| Component | Version | Real or stand-in |
|---|---|---|
| Keycloak | 26.7.5, realm `corp` | real |
| Open WebUI | 0.12.5 | real |
| LiteLLM | 1.98.0, the T1 hook `t1/t1_token_hook.py` with `T1_API=platform` | real |
| Claude Code | 2.1.325, `mm-auth` (`t3/plugin`) with `api_style: platform` | real; `mm-auth` is the T3 prototype |
| memmachine-gateway | MemVerge/MemMachine-Platform main `b38410e`, `cmd/memmachine-gateway`. Phase X runs it with `gateway-audience-flag.patch` | real |
| MemMachine core | `memmachine/memmachine:0.4.1-a1`, the image the chart pins, with the chart's `cfg.yml` (`e2e/core-cfg.yml`): semantic memory off, RRF (identity and BM25) reranking | real |
| Storage | Postgres 16 (pgvector image), Qdrant 1.20.0, the chart's version | real |
| Embedder | DeepInfra `BAAI/bge-m3`, 1024 dimensions, multilingual, through core's OpenAI embedder (`e2e/core-cfg-deepinfra.yml`). The chart defaults to OpenAI's text-embedding-3-small; this is another real model behind the same API. `e2e/core-cfg.yml` runs all-MiniLM-L6-v2 locally instead (`e2e/embed_server.py`), with the same results (run `97ba3e`) | real |
| Chat model | Phases M and X: `t1/fake_provider.py`, which answers "OK: \<prompt\>" and lists every memory line it was given, so the checks see exactly what memory reached the model. Phase R: DeepInfra `meta-llama/Llama-3.3-70B-Instruct-Turbo` through LiteLLM | stand-in (M, X), real (R) |

The platform's own Keycloak realm, Envoy, the console, quotas and the Kubernetes deployment are not
part of this run. The gateway checks tokens from the T1 Keycloak's `corp` realm, which plays the
customer's identity provider.

## Results (`e2e/e2e_check.py`, run `5fc3f7`, bge-m3)

**Phase M: the gateway exactly as on main; the hook forwards each user's own token** (#1561's fallback mode)

| ID | Check | Result |
|---|---|---|
| M1 | Open WebUI: alice's four turns are stored in core, one episode per message (8) | PASS |
| M2 | Open WebUI: alice asks in a new chat, in other words ("What kind of pet do I have…"). Her fact, stored among three unrelated ones, comes first of the 5 memories the model gets | PASS |
| M3 | Open WebUI: bob, who has memories of his own, gets his own fact first and none of alice's | PASS |
| M4 | Claude Code, carol, `mm-auth` straight to the gateway: stored in one session, ranked first in a new session asked in other words | PASS |
| M5 | The same through LiteLLM's pass-through route, with LiteLLM checking its own key (Option A, the shared route-only key) | PASS |
| M6 | The same through LiteLLM's exact routes with no LiteLLM key (Option B) | PASS |
| M5/6 | LiteLLM's callbacks saw the pass-through calls (7) | PASS |
| M7 | One person across clients: alice, signed in to `mm-auth`, asks Claude Code "What breed is my dog?" The fact she told Open WebUI comes first | PASS |
| M8 | Isolation across clients: carol's Claude Code gets none of alice's or bob's memory | PASS |
| M9 | No JWT in the model provider's 15 requests or LiteLLM's 21 spend-log rows. Claude Code's model calls are billed to the developer's own LiteLLM key | PASS |
| G | INFO, the tokens main's gateway accepts. Accepted (200): alice's corporate token (`aud` litellm, account), **a client's own client-credentials token (no person)**. Refused (401): **alice's token exchanged for the platform (`aud` memmachine-platform)**, an ID token, a forged signature, no token | INFO |

**Phase R: as phase M, with a real chat model** (DeepInfra's Llama 3.3 70B)

| ID | Check | Result |
|---|---|---|
| R1 | Open WebUI: alice gives her parking spot in one chat. In a new chat, "Where do I park at the office?" gets "P3-5fc3f7" | PASS |
| R2 | Open WebUI: alice gives her favourite food in Korean (김치찌개). Asked in English, the model answers "Your favorite food is Kimchi Jjigae, and the memo code is 5fc3f7-K." | PASS |
| R3 | Claude Code, carol, `llama` on her own LiteLLM key: told the staging server's name in one session, answers "falcon-5fc3f7" in the next | PASS |

**Phase X: the gateway patched to accept the platform audience alone; the hook exchanges Open WebUI's token** (#1561's recommended mode)

| ID | Check | Result |
|---|---|---|
| X1 | Open WebUI in exchange mode: stored, then ranked first in a new chat | PASS |
| X2 | The platform got only exchanged tokens. The hook made 6 exchanges, 3 of them from its cache, and every memory call was served. Alice's corporate token sent straight to the gateway gets 401 | PASS |
| X3 | Claude Code: `mm-auth`'s token (`aud` memmachine-platform, account) stores and recalls through the patched gateway | PASS |
| X4 | INFO, the tokens the patched gateway accepts. Accepted: the exchanged token. Refused: the corporate token, the client-credentials token, the ID token, a forged signature, no token | INFO |

### Active cross-tenant attempts (`e2e/xtenant_check.py`, 12/12 PASS, runs `c9e9c4` and `e8c7b2`)

M3 and M8 show only that Bob's ordinary questions never surfaced Alice's memory, which a search that
merely failed to match would also show. Here Bob attacks on purpose. Every request carries his own
valid token and names Alice's `org_id`, `project_id` or episode IDs, against main's gateway, directly
and through both pass-through options.

A pass needs two things:

- Bob gets nothing of Alice's back.
- **Alice's space is unchanged**, read from core's own database (`episodestore` rows whose
  `session_key` is Alice's), not from the API under attack.

| ID | Bob's attempt | Result |
|---|---|---|
| R (×3) | Search and list naming Alice's org and project, or her project alone: directly, through Option A, and through Option B | 200, Bob's own memory only, nothing of Alice's |
| F | Search with filters naming Alice's session or org (`session_key = '…'`, `org_id = '…'`, `1 = 1`, an `OR`) | 422 for the three core rejects; 200 with nothing of Alice's for the one it accepts |
| W (×3) | Store naming Alice's org and project (and Alice as producer): directly, through Option A, and through Option B | 200, stored **in Bob's org**, in a project named with Alice's id, not in Alice's space (core's database) |
| D | Delete Alice's episode by its id, plain and naming Alice's org and project | 404 "not found" (core checks each episode's session before deleting), the episode still there |
| P | Get, count and delete the project, naming Alice's | They act on Bob's own project of that name: a count of 3, his own writes, not Alice's 11. Alice's project intact |
| T | Bob's token altered to claim Alice: `sub` changed with the signature kept, the same with `alg: none`, `aud` and `azp` changed | 401 |
| A | Alice's space after all of the above | Identical rows before and after; no trace of Bob's markers |
| A2 | Alice searches her own memory | Found |

How it holds: the gateway sets `org_id` from the token's `sub` on every memory call and ignores the
`org_id` a request sends. A `project_id` from the request is used only inside the caller's own org.
Core deletes an episode only if it belongs to the caller's session.

Bob can create projects of any name in his own org. A project named with Alice's id is still his.

The Open WebUI hook and `mm-auth` send no `org_id` or `project_id` (personal memory), so a person
reaches only the space their token names.

Regression after the client changes: T1 core 38/38 and T3 13/13 still pass against the stand-in,
which stays each client's default.

## Findings

1. **Main's gateway refuses #1561's exchanged tokens.** It accepts only tokens whose `aud` names
   `account`: `SetupJWTValidator(…, []string{"account"})` in
   `internal/memmachine/gateway/server.go`. Keycloak's exchange for the platform gives a token whose
   `aud` is `memmachine-platform` alone, so the exchange mode the doc recommends gets 401 on every
   memory call. Forwarding works only because Keycloak's default scopes happen to add `account`.
   That applies to any client's token in the realm, ID tokens excepted.
   - Making the audience a setting (`gateway-audience-flag.patch`, 4 lines) and setting it to
     `memmachine-platform` is enough for exchange end to end (X1 to X3).
   - It also refuses forwarded corporate tokens, so #1561's two modes need different gateway
     settings.
2. **Main's gateway accepts a client's own token as a person.** A client-credentials token with
   `aud` `account` was served (G): the gateway took its `sub`, the client's service-account user,
   as a person's account. #1561's marker rule (no
   `client_id` claim) and its list of honoured clients (`azp`) are not built yet. Under the patched
   audience the same token was refused, but only because its audience differs.
3. **The real API has no binding.** The gateway's public API is `/v2/memories`,
   `/v2/memories/search` and so on, at the root of the memmachine-api host, not under `/api/v2`. It
   takes `org_id` from `sub`, defaults `project_id` to `sub`, and has nothing like #1561's turn
   binding (`binding`, `binding/check`, `turn-outdated`). Both clients were adapted:
   - search becomes `/v2/memories/search` (`top_k` 5, episodic);
   - store becomes `/v2/memories` with the two messages;
   - a re-check becomes `/v2/projects/list`, which only asks whether the token is still accepted.

   The binding rules T1c checked against the stand-in are therefore design-only.
4. **Real semantic recall works, and search always fills `top_k`.**
   - Every fact asked about in other words was ranked first among unrelated memories (M2, M4–M7,
     X1, X3).
   - Core returns `top_k` episodes however weak the match, so unrelated memories reach the model
     with every prompt. The memory block's size limit (T3 finding 1) is therefore always in play.
   - A question close to an earlier stored question can outrank the fact itself: the first M7
     wording did, and was replaced.
   - bge-m3 matched across languages: a Korean fact was found by an English question (R2).
   - A stored episode shows in the list API at once but reaches search a few seconds later. In one
     replay, a store listed at 4.5 s was first found by search at 7.9 s. A person who asks right after
     telling a fact may not get it yet. The checks wait on the list API, then 4 s more.
5. **A search right after a project is deleted gets 500 for about a second.** Core raises
   `SessionDeletedError` while it finishes the delete. After that, search returns empty results,
   and the next store recreates the default project. This was seen when resetting test users; it
   is not part of #1561.

## How it was run

```
set -a; . $T1_ENV/real/deepinfra.env; set +a                                      # DEEPINFRA_API_KEY
CORE_CFG=./core-cfg-deepinfra.yml \
docker compose -f e2e/docker-compose.yml --env-file $T1_ENV/real/e2e.env up -d   # Postgres 15433, Qdrant 16333, core 18081
e2e/svc.sh start all                                                              # embedder 18210, gateway 18110 (gauge 18119)
$PY e2e/e2e_check.py                       # restarts the gateway and LiteLLM per phase, then restores T1's set-up
```

- **Gateway build:** `go build ./cmd/memmachine-gateway` in `golang:1.27`, from a clone at
  `$T1_ENV/real/MemMachine-Platform`. The patched build is the same clone with
  `gateway-audience-flag.patch` applied; it is not pushed anywhere.
- **Configuration:** `e2e/litellm-config.yaml` is `t1/litellm-config.yaml` with the pass-through
  routes aimed at the real gateway, plus `memmachine-llama` (Open WebUI) and `llama` (Claude Code) on
  DeepInfra.
- **Changing the embedder** needs empty stores (`down -v`), as the chart's own guard says.
- **Secrets** stay in `$T1_ENV/real` (mode 600): `e2e.env`, `deepinfra.env`, each person's
  `mm-auth` home and configuration, and the developer keys `e2e-dev-alice` and `e2e-dev-carol`
  (models `fake` and `llama`).
- **Network:** the gateway listens on every interface, because main hard-codes `:port`. It accepts
  only the local test realm's tokens. Everything else binds to 127.0.0.1.

## Limits

- **Chat model:** a stand-in in phases M and X. Its answers echo the prompt and the memory lines, so
  the stored answers also carry earlier memories. That makes the memory blocks grow from turn to turn
  (up to about 110 lines here). It does not change which fact ranks first.
  - Llama, given those stored "OK: …" answers as memory, sometimes copied their style.
  - Phase R checks only that the answer names the fact.
- **Leak checks:** the token checks (M9) see the stand-in provider's requests; DeepInfra's side is
  not visible. LiteLLM forwards no user headers to providers (`forward_llm_provider_auth_headers`
  off).
- **Embedder:** bge-m3, not the deployment's own embedding model.
- **Not run:**
  - the platform's own Envoy, Keycloak realm, console and quotas, and core on Kubernetes;
  - semantic memory, which the chart keeps off;
  - several gateway or LiteLLM replicas.
- **Spend logs:** LiteLLM writes them in batches. This run's M9 found carol's Claude Code calls;
  alice's single M7 call had not been written yet.
- **Not run:** the clients' handling of a hung real gateway or core. That was run against the stand-in
  in T3 (F3) only.
