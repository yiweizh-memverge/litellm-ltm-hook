# T2 pre-work: Keycloak version matrix for approach A

**Date:** 2026-11-12 · **Design:** MemVerge/MemMachine-Platform#1561 at `ff0c22e` · **Script:** `t2/kc_matrix.py`, `t2/summarize.py`

Which Keycloak versions provide what approach A asks of the customer's provider, so SK hynix's answer
to "which Keycloak version" gives a verdict at once. Each version ran in a throwaway container
(`quay.io/keycloak/keycloak:<version>`, `start-dev`) from a clean realm like #1561's. Every check
issued, used and refreshed real tokens and had a refusal case. mm-auth signed in by a real PKCE code
flow, with DPoP proofs from a software P-256 key. Raw results: `$T1_ENV/logs/kc-matrix.jsonl`
(claim names and selected values, never a token).

## Result

| Capability | 22.0.5 | 24.0.5 | 25.0.6 | 26.0.8 | 26.1.5 | 26.2.5 | 26.3.5 | 26.4.7 | 26.5.7 | 26.6.4 | 26.7.5 | 26.8.1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Basic token contract | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Refresh-only DPoP (mm-auth) | no | no | no | no | no | no | no | **PASS** | PASS | PASS | PASS | PASS |
| Offline sessions | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Standard token exchange | no | no | no | no | no | **PASS** | PASS | PASS | PASS | PASS | PASS | PASS |
| Device code (conditional) | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |

**Minimum versions:** everything #1561 asks needs **Keycloak 26.4**. Token exchange alone needs
26.2. The token contract, offline sessions and device code work from 22.

| Keycloak | Verdict for approach A |
|---|---|
| 26.4 or later | No known blocker from Keycloak |
| 26.2 – 26.3 | Feasible with conditions: exchange works; mm-auth's refresh token cannot be bound alone (below) |
| 26.1 or earlier | Feasible with conditions only if SK's security team accepts forwarding the corporate token *and* one of mm-auth's options below; otherwise upgrade. A blocker if they require exchange and cannot upgrade |

## What each group checked

- **Basic token contract.** User access token RS256, `typ` Bearer, `azp`, the platform in `aud`;
  ID token `typ` ID; no `client_id` in a user token; a client-credentials token marked by `client_id`
  (in every version, 22 included); the same person's `sub` from Open WebUI's, mm-auth's and the
  console's clients; a per-client access-token lifespan (900 s) applied.
- **Refresh-only DPoP.** A `dpop-bind-enforcer` profile (`auto-configure` off,
  `allow-only-refresh-token-binding` on) applied to mm-auth by a client-roles condition. PASS means:
  the code exchange without a proof is refused; with one, the access token is a bearer (no `cnf`,
  `token_type` Bearer) and the refresh token is bound; a refresh without a proof, or with another
  key's, is refused; with the right key it succeeds and the access token is still a bearer; two
  refreshes at once both succeed (rotation off).
- **Offline sessions.** `offline_access` gives Open WebUI and mm-auth an Offline refresh token; a
  client's own offline idle limit applies (15 s set; refused after it, past Keycloak's grace window);
  a disabled person's refresh is refused.
- **Standard token exchange.** LiteLLM's confidential client exchanges a person's Open WebUI token
  for one with the platform alone as audience: same `sub`, `azp` LiteLLM, no `act`. Refused: another
  audience, a subject token not naming LiteLLM, no client secret.
- **Device code.** The device authorization request on mm-auth's client, PKCE included. The sign-in
  approved in a browser, and DPoP after it, were not run (only if headless sign-in is in phase one).

## Before 26.4: why DPoP fails, and what it would take

- 24.0.5 – 26.3.5: DPoP is a preview feature, off by default, and its executor has only
  `auto-configure`. 22.0.5 has no DPoP at all.
- With the preview turned on (`--features=dpop`, run on 26.3.5 and 24.0.5) the policy enforces a
  proof, but **binds the access token too** (`token_type` DPoP, `cnf`). The
  `allow-only-refresh-token-binding` setting is **saved without error and ignored**. A bound access
  token cannot be forwarded as a bearer by the hook, so this does not meet #1561.
- mm-auth's options there, each a condition for SK: rotate refresh tokens instead, which on
  Keycloak is realm-wide and breaks the console's Envoy session (T9); or leave mm-auth's refresh
  token neither bound nor rotated, which RFC 9700 does not allow for a public client and SK's
  security team would have to accept; or upgrade to 26.4.

## Before 26.2: exchange

26.0.8 and 26.1.5 answer `unsupported_grant_type`; only the legacy token exchange exists, a disabled
preview, which #1561 rejects because it can impersonate. 22.0.5 answers "Feature not enabled".

## Findings

1. **LiteLLM can exchange its own service-account token, and the result is not marked.** With a
   service account on LiteLLM's client (26.2 – 26.8.1), exchanging LiteLLM's own client-credentials
   token gives a platform-audience token for `service-account-litellm` with **no `client_id`**, so
   the gateway's marker rule ("a token with `client_id` has no person") lets it through as a person.
   It reaches no employee's memory, but it breaks #1561's "no token without a person". With the
   service account off, people's exchanges still work and LiteLLM gets no token of its own (401).
   So: LiteLLM's client needs **no service account**. #1561 at `520b19c` assumes it has one (the
   marker rule "is what refuses LiteLLM's own where it exchanges tokens").
2. **Re-enabling a disabled person revives their old offline tokens** (every version). Disabling
   cuts memory off; re-enabling, by mistake or on rehire, brings back sign-ins from before. Relevant
   to offboarding (T5): revoke the person's offline sessions too, not only disable.
3. **An exchanged token lives LiteLLM's client's lifespan** (300 s here), whatever its subject had
   left (900 s), as #1561 says.

## Not covered

Red Hat build of Keycloak (needs a registry login; its 24, 26.0, 26.2 and 26.4 follow the community
releases of the same number, not run); production mode, clustering and a database; the device-code
sign-in completed in a browser, with DPoP; Envoy with rotation (T9, needs a real Envoy); SK's own
version, when known.
