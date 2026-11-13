"""Judge $T1_ENV/logs/kc-matrix.jsonl: one verdict per capability group and version.

    $PY t2/summarize.py            # markdown table, then every check that is not ok

Each check is ok, FAIL, ERROR (a 5xx, an exception, a missing answer: the test did not get a
verdict) or not run (a record from an earlier schema that lacks it). A refusal check is ok only
with the expected HTTP status and OAuth error code; any other answer is FAIL, a 5xx is ERROR.
A group is PASS when every check is ok, PASS* when every check present is ok but some were not
run, otherwise FAIL or ERROR. The latest record per (version, features flag) wins.
"""
import json
import os
from pathlib import Path

T1_ENV = Path(os.environ.get("T1_ENV", Path.home() / "t1-tokenpath-env"))
SRC = T1_ENV / "logs" / "kc-matrix.jsonl"
MISSING = object()

OK, FAIL, ERROR, NOT_RUN = "ok", "FAIL", "ERROR", "not run"


def get(r, path):
    x = r
    for k in path.split("."):
        if isinstance(x, dict) and k in x:
            x = x[k]
        elif isinstance(x, list) and k.isdigit() and int(k) < len(x):
            x = x[int(k)]
        else:
            return MISSING
    return x


def refused(x, statuses, errors):
    """A refusal: the expected status and OAuth error, nothing else."""
    if x is MISSING:
        return NOT_RUN, "not run"
    if not isinstance(x, dict) or "status" not in x:
        return ERROR, f"no answer {x}"
    st, e = x["status"], (x.get("error") or {})
    code = e.get("error") if isinstance(e, dict) else None
    if st >= 500:
        return ERROR, f"{st} {e}"
    if st in statuses and code in errors:
        return OK, f"{st} {code}"
    return FAIL, f"expected {sorted(statuses)} {sorted(errors)}, got {st} {e}"


def succeeded(x):
    if x is MISSING:
        return NOT_RUN, "not run"
    st = x.get("status") if isinstance(x, dict) else x
    if st == 200:
        return OK, "200"
    if isinstance(st, int) and st >= 500:
        return ERROR, str(x)
    return FAIL, str(x)[:160]


def equals(x, want, label):
    if x is MISSING:
        return NOT_RUN, "not run"
    return (OK, f"{label}={x}") if x == want else (FAIL, f"{label}={x}, want {want}")


GRANT = {400}
DPOP_ERR = {"invalid_grant", "invalid_dpop_proof"}
EXCH_ERR = {"invalid_request", "invalid_target", "access_denied"}


def platform_only(x):
    if x is MISSING:
        return NOT_RUN, "not run"
    return (OK, f"aud={x}") if x in ("memmachine-platform", ["memmachine-platform"]) else (FAIL, f"aud={x}")


def c_contract(r):
    return [
        ("user token RS256", equals(get(r, "contract.user_token.alg"), "RS256", "alg")),
        ("user token typ Bearer", equals(get(r, "contract.user_token.typ"), "Bearer", "typ")),
        ("ID token typ ID", equals(get(r, "contract.user_token.id_token_typ"), "ID", "typ")),
        ("user token has no client_id", equals(get(r, "contract.user_token.client_id_claim"), False, "client_id")),
        ("per-client lifespan 900 s", equals(get(r, "contract.user_token.lifespan_s"), 900, "lifespan")),
        ("client-credentials token marked by client_id",
         equals(get(r, "contract.client_credentials_token.client_id"), True, "client_id")),
        ("same sub across clients", equals(get(r, "contract.same_sub_across_clients"), True, "same")),
        ("mm-auth token names the platform",
         equals("memmachine-platform" in (get(r, "contract.platform_audience_mm_auth") or []), True, "aud")),
    ]


def c_dpop(r):
    checks = [
        ("executor has allow-only-refresh-token-binding",
         equals("allow-only-refresh-token-binding" in (get(r, "server.dpop_executor_options") or []), True, "option")),
        ("code exchange without proof refused",
         refused(get(r, "dpop.code_exchange_without_proof_refused"), GRANT, DPOP_ERR)),
        ("code exchange with proof", succeeded(get(r, "dpop.code_exchange_with_proof"))),
        ("access token a bearer at sign-in", equals(get(r, "dpop.code_exchange_with_proof.access_cnf"), False, "cnf")),
        ("refresh token bound to the key",
         equals(get(r, "dpop.code_exchange_with_proof.refresh_bound_to_key1"), True, "jkt")),
        ("refresh without proof refused", refused(get(r, "dpop.refresh_without_proof_refused"), GRANT, DPOP_ERR)),
        ("refresh with another key refused", refused(get(r, "dpop.refresh_other_key_refused"), GRANT, DPOP_ERR)),
        ("refresh with the key", succeeded(get(r, "dpop.refresh_right_key"))),
        ("access token a bearer after refresh", equals(get(r, "dpop.refresh_right_key.access_cnf"), False, "cnf")),
    ]
    for stage in ("after_refresh", "after_concurrent"):
        checks += [
            (f"{stage}: returned refresh token bound to the key", equals(get(r, f"dpop.{stage}.bound_to_key1"), True, "jkt")),
            (f"{stage}: without proof refused", refused(get(r, f"dpop.{stage}.without_proof"), GRANT, DPOP_ERR)),
            (f"{stage}: another key refused", refused(get(r, f"dpop.{stage}.other_key"), GRANT, DPOP_ERR)),
            (f"{stage}: the key works", succeeded(get(r, f"dpop.{stage}.right_key"))),
            (f"{stage}: access token a bearer", equals(get(r, f"dpop.{stage}.right_key.access_cnf"), False, "cnf")),
        ]
    rounds = get(r, "dpop.concurrent_rounds")
    if rounds is MISSING:
        checks.append(("concurrent refreshes", NOT_RUN_PAIR))
    else:
        all_ok = all(x["statuses"] == [200, 200] and x["returned_bound_to_key1"] == [True, True] for x in rounds)
        overlapped = sum(x["overlapped"] for x in rounds)
        checks.append(("concurrent refreshes succeed, returned tokens bound",
                       (OK if all_ok else FAIL, f"{len(rounds)} rounds, {overlapped} overlapped")))
    return checks


NOT_RUN_PAIR = (NOT_RUN, "not run")


def c_offline(r):
    checks = [
        ("Open WebUI gets an offline token", equals(get(r, "offline.open_webui_offline.refresh_typ"), "Offline", "typ")),
        ("mm-auth gets an offline token", equals(get(r, "offline.mm_auth_offline.refresh_typ"), "Offline", "typ")),
        ("idle limit: refresh within it", succeeded(get(r, "offline.client_offline_idle_limit.refresh_within_limit"))),
        ("idle limit: refresh after it refused",
         refused(get(r, "offline.client_offline_idle_limit.refresh_after_limit"), GRANT, {"invalid_grant"})),
        ("token valid before disabling", succeeded(get(r, "offline.refresh_while_enabled"))),
        ("disable applied", equals(get(r, "offline.disable_user"), 204, "status")),
        ("disabled person's refresh refused", refused(get(r, "offline.refresh_after_disable_refused"), GRANT, {"invalid_grant"})),
        ("re-enable applied", equals(get(r, "offline.enable_user"), 204, "status")),
    ]
    return checks


def c_offline_max(r):
    log = get(r, "offline.client_offline_max.refreshes")
    if log is MISSING:
        return [("offline maximum", NOT_RUN_PAIR)]
    mx = get(r, "offline.client_offline_max.max_s")
    before = [x for x in log if x["t_s"] < mx]          # the session starts at sign-in, just before t=0
    first_refusal = next((x for x in log if x["status"] != 200), None)
    checks = [("refreshes before the maximum succeed",
               (OK if before and all(x["status"] == 200 for x in before) else FAIL, f"{[x['status'] for x in before]}"))]
    if first_refusal is None:
        checks.append(("refused after the maximum", (FAIL, f"never refused in {log[-1]['t_s'] if log else 0} s")))
    else:
        v, why = refused(first_refusal, GRANT, {"invalid_grant"})
        if v == OK and first_refusal["t_s"] < mx:
            v, why = FAIL, f"refused at {first_refusal['t_s']} s, before the maximum"
        checks.append(("refused after the maximum", (v, f"at {first_refusal['t_s']} s: {why}")))
    return checks


def c_exchange(r):
    tp = get(r, "exchange.to_platform")
    if tp is MISSING or (isinstance(tp, dict) and tp.get("status") != 200):
        e = tp.get("error") if isinstance(tp, dict) else tp
        return [("standard exchange to the platform", (FAIL, f"not available: {e}"))]
    return [
        ("exchange to the platform", succeeded(tp)),
        ("same person", equals(get(r, "exchange.to_platform.same_sub"), True, "same_sub")),
        ("azp LiteLLM", equals(get(r, "exchange.to_platform.azp"), "litellm", "azp")),
        ("platform-only audience", platform_only(get(r, "exchange.to_platform.aud"))),
        ("no act claim", equals(get(r, "exchange.to_platform.act"), False, "act")),
        ("another audience refused", refused(get(r, "exchange.other_audience_refused"), {400, 403}, EXCH_ERR)),
        ("subject not naming LiteLLM refused",
         refused(get(r, "exchange.subject_not_naming_litellm_refused"), {400, 403}, EXCH_ERR)),
        ("no client secret refused",
         refused(get(r, "exchange.no_client_secret_refused"), {400, 401}, {"unauthorized_client", "invalid_client"})),
    ]


def c_exchange_sa_off(r):
    if get(r, "exchange.to_platform.status") != 200:
        return [("exchange with LiteLLM's service account off", (FAIL, "no standard exchange"))]
    return [
        ("people's exchange", succeeded(get(r, "exchange.service_account_off.people_exchange"))),
        ("same person", equals(get(r, "exchange.service_account_off.people_exchange.same_sub"), True, "same_sub")),
        ("platform-only audience", platform_only(get(r, "exchange.service_account_off.people_exchange.aud"))),
        ("another audience refused",
         refused(get(r, "exchange.service_account_off.other_audience_refused"), {400, 403}, EXCH_ERR)),
        ("LiteLLM's own token refused",
         refused(get(r, "exchange.service_account_off.own_token_refused"), {400, 401}, {"unauthorized_client", "invalid_client"})),
        ("a service token from before refused",
         refused(get(r, "exchange.service_account_off.service_token_from_before_refused"), {400, 401, 403},
                 EXCH_ERR | {"invalid_token", "invalid_grant", "unauthorized_client"})),
    ]


def c_device(r):
    return [("device authorization request accepted", succeeded(get(r, "device"))),
            ("device code returned", equals(get(r, "device.has_device_code"), True, "device_code"))]


GROUPS = [
    ("Basic token contract", c_contract),
    ("Refresh-only DPoP, kept across refreshes (mm-auth)", c_dpop),
    ("Offline token, idle limit, disable", c_offline),
    ("Offline maximum while in use", c_offline_max),
    ("Standard token exchange", c_exchange),
    ("Exchange with LiteLLM's service account off", c_exchange_sa_off),
    ("Device authorization request accepted (not a sign-in)", c_device),
]


def verdict(checks):
    vs = [v for _, (v, _) in checks]
    if ERROR in vs:
        return "ERROR"
    if FAIL in vs:
        return "FAIL"
    if NOT_RUN in vs:
        return "PASS*" if OK in vs else "not run"
    return "PASS"


def notes(r):
    out = []
    if get(r, "offline.old_offline_token_after_reenable.status") == 200:
        out.append("re-enabling a disabled person revives their old offline token")
    s = get(r, "exchange.own_service_token_as_subject")
    if isinstance(s, dict) and s.get("status") == 200:
        out.append(f"with a service account, LiteLLM exchanges its own token: client_id={s.get('client_id_claim')} "
                   f"user={s.get('preferred_username')} aud={s.get('aud')}")
    sh = get(r, "exchange.short_subject")
    if isinstance(sh, dict) and sh.get("status") == 200:
        out.append(f"subject with {sh['subject_left_s']} s left -> exchanged token lives {sh['exchanged_lifespan_s']} s, "
                   f"outliving its subject by {sh['exchanged_outlives_subject_s']} s")
    log = get(r, "offline.client_offline_max.refreshes")
    if log is not MISSING and log:
        out.append(f"offline maximum {get(r, 'offline.client_offline_max.max_s')} s: refresh statuses by time "
                   + ", ".join(f"{x['t_s']}s:{x['status']}" for x in log))
    return out


def main():
    recs = {}
    for line in SRC.read_text().splitlines():
        r = json.loads(line)
        recs[(r["version"], r.get("features_flag"))] = r
    keys = sorted(recs, key=lambda k: (tuple(int(x) for x in k[0].split(".")), k[1] or ""))
    cols = [f"{v}{' +' + f if f else ''}" for v, f in keys]
    print("| Capability | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    detail = []
    for name, fn in GROUPS:
        cells = []
        for k in keys:
            r = recs[k]
            if not r.get("started"):
                cells.append("did not start")
                continue
            checks = fn(r)
            cells.append(verdict(checks))
            detail += [f"- {k[0]}{' +' + k[1] if k[1] else ''} · {name} · {c}: {v} ({why})"
                       for c, (v, why) in checks if v != OK]
        print(f"| {name} | " + " | ".join(cells) + " |")
    print("\nPASS* = every check present passed; some checks were added after this version ran (schema "
          "1) and are not run.")
    if detail:
        print("\nNot ok:\n" + "\n".join(detail))
    print("\nNotes:")
    for k in keys:
        for n in notes(recs[k]) if recs[k].get("started") else [str(recs[k].get("start_error", ""))[:300]]:
            print(f"- {k[0]}{' +' + k[1] if k[1] else ''}: {n}")


if __name__ == "__main__":
    main()
