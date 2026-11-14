# T3 report: Claude Code + `mm-auth`, the minimal real loop

**Date:** 2026-11-13 · **Branch:** `validate/t1-token-path` · **Design:** MemVerge/MemMachine-Platform#1561 at `086cc62`

**Result:** a minimal `mm-auth` (`t3/plugin/mm_auth.py`) loaded into real Claude Code as a plugin
works end to end against the T1 stack's real Keycloak. It handles sign-in, refresh, memory added
on each prompt, the turn stored when it ends, sessions sharing one sign-in, and failures that never
stop Claude Code. Memory calls work both directly to the platform and through LiteLLM's
pass-through route. The model calls carried no corporate token.

Two findings for #1561: Claude Code passes a hook's `additionalContext` to the model whole only up
to 10,000 characters (finding 1), and LiteLLM's pass-through route has two workable set-ups with
different costs: LiteLLM also checks its own key (an Enterprise feature to set up cleanly), or only
the platform checks the developer's token (finding 2).

## Versions

| Component | Version |
|---|---|
| Claude Code | 2.1.323 (#1561 pins 2.1.319), `claude -p`, plugin loaded with `--plugin-dir`, its own `CLAUDE_CONFIG_DIR` |
| Keycloak | 26.7.5, realm `corp`, client `mm-auth` from `t3/t3_setup.py`: public, PKCE S256, loopback redirect, offline_access, device grant on, platform audience, 60 s access tokens, refresh token bound by DPoP (`dpop-bind-enforcer`, refresh-only) |
| LiteLLM | 1.98.0, Claude Code's `/v1/messages` to the fake provider (`use_chat_completions_url_for_anthropic_messages`), pass-through route `/memmachine` with `auth: true` |
| Gateway, model provider | T1 stand-ins |

## Results (`t3/t3_check.py`, 13/13 PASS; `--via-litellm`, 6/6 PASS)

| ID | Check | Result |
|---|---|---|
| L1 | Sign-in: PKCE code flow back to a random loopback port. Gets an offline refresh token bound to the installation's key (`cnf.jkt` = the key's thumbprint), and a bearer access token (`azp` mm-auth, `aud` the platform, no `cnf`). Files are 600 in a 700 directory | PASS |
| L2 | An authorization request without PKCE is refused (`invalid_request`) | PASS |
| L3 | A refresh with the key's proof gets 200. With no proof it is refused (400 `invalid_dpop_proof`), and with another key's proof too (400 `invalid_grant`) | PASS |
| H1 | A real turn. `UserPromptSubmit` searches and adds memory as `additionalContext`. `Stop` stores the prompt and the final answer, exactly as Claude Code printed it; the answer comes from the hook input's `last_assistant_message` | PASS |
| H2 | A second session on the same sign-in gets the first session's memory in its model request and answer | PASS |
| H4 | More memory than Claude Code passes on whole: the block stays under the limit (8,051 characters, 4 of 5 episodes kept) and the newest is kept | PASS |
| C1 | Access token expired, then three sessions at once: one refresh (the other two waited on the lock and used its token), all three served and stored, no new sign-in | PASS |
| H3 | Model calls carry no corporate token. The provider saw only its own key, and Claude Code sent LiteLLM no `Authorization` header. None of the run's `mm-auth` tokens appeared in the provider's or LiteLLM's logs or LiteLLM's database. The gateway saw `azp` mm-auth | PASS |
| K1 | Claude Code's model calls use an ordinary developer's LiteLLM key (`ANTHROPIC_API_KEY`), not the master key: `user_id` carol, models `["fake"]`, no admin role (`/key/generate` with it: 401). LiteLLM's spend logs put all 6 of the run's Claude Code requests on carol's key and none on any other | PASS |
| F1 | Not signed in: Claude Code answers and shows "memory is off: not signed in. Run `mm-auth login` in a terminal." The gateway is not called | PASS |
| F2 | Refresh refused (the offline session revoked at Keycloak): Claude Code answers and shows "your sign-in was refused (invalid_grant). Run `mm-auth login` again". There is one refresh attempt and no retries. A new sign-in brings memory back | PASS |
| F3 | The platform hangs (gateway `SIGSTOP`). The first prompt waited 5.0 s, the hook's deadline; the next waited 8 ms, because a backoff was in place. Both were answered, with "memory unavailable …; retrying automatically" | PASS |
| F3 | After the backoff, one probe finds the platform back, and memory and stores return | PASS |
| P-H1..H4, P-K1 | H1–H4 and K1 again with `mm-auth`'s memory calls through LiteLLM's pass-through route, using a shared route-only key: all PASS. The gateway still received `mm-auth`'s token unchanged, and no `mm-auth` token was in LiteLLM's logs or database | PASS |

"Shown" means the hook's `systemMessage`. It is read from `claude -p --output-format stream-json
--include-hook-events`; the interactive screen was not looked at.

## Findings

1. **Claude Code passes `additionalContext` whole only up to 10,000 characters.** On 2.1.323, a
   10,000-character context reached the model as it was. One of 10,001 or more was saved to a
   file under the session's `tool-results/` directory, and the model got a `<persisted-output>`
   note with only the first 2,000 characters. Without a cap, the memory ranked or placed later is
   silently lost. Here that was the newest episode, and H2 failed until the cap was added. #1561
   sets no size for the memory block; `mm-auth`, and likely LiteLLM's hook for its own model,
   need a character budget. The prototype keeps whole episodes up to 9,000 characters and cuts
   any single episode at 2,000.
2. **LiteLLM's pass-through route has two workable set-ups, a trade-off.** #1561 gives the principle (the
   developer's token forwarded unchanged; where LiteLLM keeps its own keys, one asked for on the route) but
   not LiteLLM's configuration. In 1.98.0 with a key database:
   - **Option A, LiteLLM also checks its own key** (`auth: true`, #1561's wording). A non-admin key is
     refused (403) unless the route is granted to it or its team (`allowed_passthrough_routes`). A key
     limited by `allowed_routes` alone also gets 403; only the master key passes without the grant.
     Setting `allowed_passthrough_routes` as a key field is refused as an Enterprise feature; written
     into the key's or team's `metadata` it was accepted and worked unlicensed. LiteLLM's route check
     reads it from there, but this looks like a gap rather than a supported path. With the grant, a
     shared key that also has `allowed_routes: ["/memmachine/*"]` reaches only the route (model calls
     get 403). LiteLLM then ties each memory call to the key it carries, for its rate limits and
     request logs. The shared route key tested here counts all developers together. A developer's own
     key, granted the route, would count each one, but `mm-auth` would need that key, and an admin to
     grant every key or its team.
   - **Option B, LiteLLM checks no key** (`auth: false`), and the platform's token check is the only
     guard. It works unlicensed, but LiteLLM honours `auth: false` only when the request path equals the
     route's `path`: a prefix route with `include_subpath` falls back to key checks (a plain key got
     401). So each endpoint `mm-auth` calls needs its own exact route (`/memmachine-open/api/v2/memories/search`
     and `/memmachine-open/api/v2/memories` in `t1/litellm-config.yaml`); any other path got 404. The
     exposure is the same as calling the platform directly. LiteLLM reads no key on these routes, so it
     cannot tie a memory call to anyone, even if a developer's own key is sent along. Its rate limits
     and per-key logs do not apply.
   - Memory calls cost no model tokens, so LiteLLM's spend is 0 in both options. In the real-platform run
     (`e2e/`), LiteLLM's spend logs held Option A's calls under the shared route key and Option B's
     under no key. In both, the platform counts and limits memory calls per person by the token's
     `sub`, whichever route they take.
   - In both, a request without a developer's token reached no memory (`no-bearer`), the token reached
     the gateway unchanged, and LiteLLM kept none of the run's tokens. `t3_check.py --via-litellm` ran
     Option A; Option B was checked with direct requests.
   - Not tested: LiteLLM's custom-auth mode. From the source, a custom auth function's result skips this
     route check unless `enable_post_custom_auth_checks` is on.
3. **LiteLLM sends `/v1/messages` to the Responses API for `openai/` models by default** (1.98.0).
   A provider that serves only chat completions needs `use_chat_completions_url_for_anthropic_messages`.
   This only affects which LiteLLM settings SK uses for Claude Code, not #1561.
4. Claude Code's `Stop` input carries `last_assistant_message`, so the store does not need to read
   the transcript. `UserPromptSubmit` carries `prompt`, `session_id`, `prompt_id` and `cwd`.

## Prototype limits (not #1561's full `mm-auth`)

- Python, not one signed program per OS.
- No OS credential store: a 600 file, which is #1561's fallback on headless Linux.
- The refresh runs in the hook's process with a timeout of its own, not in a detached process that
  outlives the hook.
- `mm-auth`'s access token also names `account` in `aud`: the test client keeps Keycloak's default
  scopes, where #1561 turns "Full scope allowed" off and drops the `roles` scope. The gateway stand-in
  checks only that the platform is named.
- No project file (personal memory only), no device-code sign-in, no job keys, and no 30-day
  expiry of turn state.
- Hooks loaded with `--plugin-dir`, not managed settings, so `allowManagedHooksOnly` and the
  distribution and update path were not tested.

## Not covered

- SSH: device-code sign-in with DPoP, or a forwarded loopback port. This waits on SK allowing the
  device grant.
- The interactive Claude Code screen, Windows and macOS keychains, and a hardware-bound key.
- A store that fails at the end of a turn, and a refresh that outlives the hook's timeout.

## Test-only setup left in place

- Keycloak: client `mm-auth` (60 s access tokens), client role `dpop-bound`, client profile and
  policy `mm-auth-dpop`.
- LiteLLM config: the Anthropic-to-chat-completions setting, the `/memmachine` pass-through route
  (Option A) and the two exact `/memmachine-open/...` routes (Option B).
- LiteLLM database: test keys `t3-*`, carol's developer key `t3-dev-carol`, and one team `t3-team-*`.
- `$T1_ENV/t3/`: `mm-auth`'s home (carol's sign-in), Claude Code's config directory, carol's
  developer key (`dev-key`) and the shared route key (600).
