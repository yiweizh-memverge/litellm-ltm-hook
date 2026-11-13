# T2 pre-work: Keycloak version matrix for approach A

**Date:** 2026-11-12 · **Design:** MemVerge/MemMachine-Platform#1561 at `ff0c22e` · **Script:** `t2/kc_matrix.py`, `t2/summarize.py`

Which Keycloak versions pass the provider checks approach A depends on, so SK hynix's answer to
"which Keycloak version" narrows the verdict at once. Each version ran in a throwaway container
(`quay.io/keycloak/keycloak:<version>`, `start-dev`) from a clean realm like #1561's. Every check
issued, used and refreshed real tokens and had a refusal case. mm-auth signed in by a real PKCE code
flow, with DPoP proofs from a software P-256 key. Raw results: `$T1_ENV/logs/kc-matrix.jsonl` (claim
names and selected values, never a token).

**How checks are judged.** A refusal is ok only with the expected HTTP status and OAuth error code
(for example `400 invalid_dpop_proof`); any other 4xx is FAIL. ERROR means the test got no verdict:
an exception in the group, a 5xx, or a result missing from a full (schema-2) record. The one
exception is a `501` naming its error ("Feature not enabled"), Keycloak's answer that a feature is
absent, which is FAIL. "Not run" is reserved for checks added after a schema-1 record was taken.
No ERROR occurred in any run. `t2/test_summarize.py` damages real records one way per case
(exceptions, 5xx, lost results, no or non-overlapping concurrent rounds, a DPoP `token_type`) and
checks the verdicts: 18 cases, all right.

## Result

| Capability | 22.0.5 | 24.0.5 | 25.0.6 | 26.0.8 | 26.1.5 | 26.2.5 | 26.3.5 | 26.4.7 | 26.5.7 | 26.6.4 | 26.7.5 | 26.8.1 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Basic token contract | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Refresh-only DPoP, kept across refreshes (mm-auth) | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | **PASS** | PASS\* | PASS\* | PASS | PASS |
| Offline token, idle limit, disable | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |
| Offline maximum while in use | — | — | — | — | — | PASS | PASS | PASS | — | — | PASS | PASS |
| Standard token exchange | FAIL | FAIL | FAIL | FAIL | FAIL | **PASS** | PASS | PASS | PASS | PASS | PASS | PASS |
| Exchange with LiteLLM's service account off | FAIL | FAIL | FAIL | FAIL | FAIL | **PASS** | PASS | PASS | PASS\* | PASS\* | PASS | PASS |
| Device authorization request accepted (not a sign-in) | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS | PASS |

PASS\* = every check present passed, but the version ran before the newer checks were added. — = not
run. Full schema-2 runs: 26.2.5, 26.3.5, 26.4.7, 26.7.5, 26.8.1. Also run: 24.0.5 and 26.3.5 with
the DPoP preview on (below).

**What it supports.** Among the versions tested, **26.4.7 is the lowest that passed both this
matrix's refresh-only DPoP checks and its standard-exchange checks**; 26.2.5 the lowest that passed
exchange. 26.4.0 – 26.4.6 were not tested, and passing this matrix is not passing all of #1561 (see
Not covered). SK's actual version and configuration still need confirming.

| SK's Keycloak, by what this matrix shows | Reading |
|---|---|
| 26.4.7 or a later tested version | Passed every Keycloak check here; the items under Not covered remain |
| 26.2 – 26.3 | Exchange passed; mm-auth's refresh token cannot be bound alone (below), so mm-auth needs a condition |
| 26.1 or earlier | No standard exchange and no refresh-only DPoP: feasible only if SK's security team accepts forwarding the corporate token and one of mm-auth's options below, otherwise an upgrade; a blocker if they require exchange and cannot upgrade |

## What each group checks

- **Basic token contract.** User access token RS256, `typ` Bearer, `azp`, the platform in `aud`; ID
  token `typ` ID; no `client_id` in a user token; a client-credentials token marked by `client_id`
  (every version, 22 included); the same person's `sub` from Open WebUI's, mm-auth's and the
  console's clients; a per-client access-token lifespan (900 s) applied.
- **Refresh-only DPoP, kept across refreshes.** A `dpop-bind-enforcer` profile (`auto-configure`
  off, `allow-only-refresh-token-binding` on) applied to mm-auth by a client-roles condition. The code
  exchange without a proof is refused (`400`); with one, the access token is a bearer (no `cnf`,
  `token_type` Bearer) and the refresh token is bound to the key (`cnf.jkt` = the key's RFC 7638
  thumbprint). A refresh without a proof (`400 invalid_dpop_proof`) or with another key
  (`400 invalid_grant`) is refused, with the right key it succeeds. **The refresh token each refresh
  returns** is checked the same way: bound to the same key, refused without a proof and with another
  key, accepted with the key, the access token still a bearer. Two refreshes released together by a
  barrier, 5 rounds: in every round both succeeded (rotation off), both returned refresh tokens were
  bound to the key, and the two client request intervals overlapped. That shows the requests were in
  flight at once, not that the server processed them in parallel. The last returned token passed the
  same checks. `token_type` Bearer is checked at sign-in and after every refresh stage.
- **Offline token, idle limit, disable.** `offline_access` gives Open WebUI and mm-auth an Offline
  refresh token; a client's own offline idle limit (15 s) is enforced (refused with
  `400 invalid_grant` after it, past Keycloak's grace window); the token refreshes before disabling;
  the disable and re-enable admin calls succeed (`204`); a disabled person's refresh is refused.
- **Offline maximum while in use.** A client's offline session maximum (30 s, realm maximum on),
  with the token refreshed every 10 s so idle never ends it: refreshes at 10 s and 20 s succeeded, the
  one at 30 s was refused (`400 invalid_grant`), with no grace window.
- **Standard token exchange.** LiteLLM's confidential client exchanges a person's Open WebUI token
  for one with the platform alone as audience: same `sub`, `azp` LiteLLM, no `act`. Refused: another
  audience (`400 invalid_request`), a subject token not naming LiteLLM (`403 access_denied`), no client
  secret (`401 unauthorized_client`).
- **Exchange with LiteLLM's service account off.** The same exchange checks with the service account
  off on LiteLLM's client, plus: LiteLLM's own client-credentials request refused
  (`401 unauthorized_client`), and a service-account token issued before it was turned off refused as
  a subject (`400 invalid_request`).
- **Device authorization request accepted.** Only the device authorization request on mm-auth's
  client, PKCE included, returns a device code. No sign-in was approved, no token was polled, no DPoP:
  this row says nothing about whether headless mm-auth sign-in works.

## Before 26.4: DPoP

- 24.0.5 – 26.3.5: DPoP is a preview feature, off by default, and its executor offers only
  `auto-configure`. 22.0.5 has no DPoP.
- With the preview on (`--features=dpop`, run on 26.3.5 and 24.0.5) the policy enforces a proof but
  **binds the access token too** (`token_type` DPoP, `cnf`). The `allow-only-refresh-token-binding`
  setting is **saved without error and ignored**. A bound access token cannot be forwarded as a
  bearer by the hook, so this does not meet #1561.
- mm-auth's options there, each a condition for SK: rotate refresh tokens instead, which on Keycloak
  is realm-wide and breaks the console's Envoy session (T9); leave mm-auth's refresh token neither
  bound nor rotated, which RFC 9700 does not allow for a public client and SK's security team would
  have to accept; or upgrade.

## Before 26.2: exchange

26.0.8 and 26.1.5 answer `unsupported_grant_type`; only the legacy token exchange exists, a disabled
preview, which #1561 rejects because it can impersonate. 22.0.5 answers "Feature not enabled".

## Findings

1. **LiteLLM can exchange its own service-account token, and the result is not marked.** With a
   service account on LiteLLM's client (26.2.5 – 26.8.1), exchanging LiteLLM's own client-credentials
   token gives a platform-audience token for `service-account-litellm` with **no `client_id`**, so the
   gateway's marker rule ("a token with `client_id` has no person") lets it through as a person. It
   reaches no employee's memory, but breaks #1561's "no token without a person". With the service
   account off, every exchange check above still passes and LiteLLM can neither get a token of its
   own nor exchange one issued before. So LiteLLM's client needs **no service account**. #1561 at
   `520b19c` assumes it has one (the marker rule "is what refuses LiteLLM's own where it exchanges
   tokens").
2. **An exchanged token outlives its subject.** A subject token with 25 s left was exchanged for one
   living 300 s, LiteLLM's client's lifespan: 275 s past its subject (26.2.5 – 26.8.1). Keycloak does
   not cap it at the subject's expiry, so the hook must cache it no longer than the earlier of the
   two, as #1561 requires.
3. **Re-enabling a disabled person revives their old offline tokens** (every version). Disabling
   cuts memory off; re-enabling, by mistake or on rehire, brings back sign-ins from before. Relevant to
   offboarding (T5): revoke the person's offline sessions too, not only disable.
4. **A client's offline maximum ends a sign-in in use**, at the limit, with no grace window (26.2.5 –
   26.8.1), so SK can cap how long mm-auth's sign-in lasts.

## Not covered

- The console client's role mapper and role claim, "Full scope allowed" off, lightweight tokens.
- The device-code sign-in completed in a browser, its token polling and DPoP after it (only if
  headless sign-in is in phase one; #1561 saw the device grant's first token differ from later ones).
- Envoy with rotation for the console session (T9): needs a real Envoy, not Keycloak results.
- 26.4.0 – 26.4.6; the newer checks on 22.0.5 – 26.1.5, 26.5.7, 26.6.4; Red Hat build of Keycloak
  (registry login needed); production mode, clustering and a database.
- SK's own version, when known.
