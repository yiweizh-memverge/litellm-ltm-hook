"""Judge $T1_ENV/logs/kc-matrix.jsonl: one verdict per capability group and version.

    $PY t2/summarize.py            # markdown table, then every check that is not ok

Each check is ok, FAIL, ERROR or not run:
  - ERROR: the test got no verdict: an exception in the group, a 5xx, or, in a schema-2 record, a
    result that should be there and is not. One exception: a 501 naming its error ("Feature not
    enabled") at a group's first call is Keycloak saying the feature is absent, so FAIL.
  - not run: a schema-1 record (the first script) lacking a check added later. Nothing else.
  - A refusal is ok only with the expected HTTP status and OAuth error code; any other 4xx is FAIL.
A group is PASS when every check is ok, PASS* when every check is ok or not run, otherwise FAIL or
ERROR (ERROR wins). The latest record per (version, features flag) wins.
"""
import json
import os
from pathlib import Path

T1_ENV = Path(os.environ.get("T1_ENV", Path.home() / "t1-tokenpath-env"))
SRC = T1_ENV / "logs" / "kc-matrix.jsonl"
MISSING = object()

OK, FAIL, ERROR, NOT_RUN = "ok", "FAIL", "ERROR", "not run"
CURRENT_SCHEMA = 2
CONCURRENT_ROUNDS = 5                       # kc_matrix.CONCURRENT_ROUNDS


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


def contains(x, item, label):
    if x is MISSING:
        return NOT_RUN, "not run"
    if not isinstance(x, (list, str)):
        return ERROR, f"{label}={x!r}"
    return (OK, f"{label} has {item}") if item in x else (FAIL, f"{label}={x}, lacks {item}")


def entry(x, label):
    """The first call of a group: 200 goes on, a 5xx or no answer is ERROR, another status FAIL."""
    if x is MISSING:
        return NOT_RUN, "not run"
    st = x.get("status") if isinstance(x, dict) else None
    if st == 200:
        return OK, "200"
    err = x.get("error") if isinstance(x, dict) else None
    if st == 501 and isinstance(err, dict) and err.get("error"):
        return FAIL, f"{label}: not supported (501 {err['error']})"     # an answer, not a failure
    if not isinstance(st, int) or st >= 500:
        return ERROR, f"{label}: {x}"
    return FAIL, f"{label}: {err}"


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
         contains(get(r, "contract.platform_audience_mm_auth"), "memmachine-platform", "aud")),
    ]


def c_dpop(r):
    checks = [
        ("executor has allow-only-refresh-token-binding",
         (FAIL, "no dpop-bind-enforcer executor") if get(r, "server.dpop_executor_options") is None
         else contains(get(r, "server.dpop_executor_options"), "allow-only-refresh-token-binding", "options")),
        ("code exchange without proof refused",
         refused(get(r, "dpop.code_exchange_without_proof_refused"), GRANT, DPOP_ERR)),
        ("code exchange with proof", succeeded(get(r, "dpop.code_exchange_with_proof"))),
        ("access token unbound at sign-in", equals(get(r, "dpop.code_exchange_with_proof.access_cnf"), False, "cnf")),
        ("token_type Bearer at sign-in", equals(get(r, "dpop.code_exchange_with_proof.token_type"), "Bearer", "token_type")),
        ("refresh token bound to the key",
         equals(get(r, "dpop.code_exchange_with_proof.refresh_bound_to_key1"), True, "jkt")),
        ("refresh without proof refused", refused(get(r, "dpop.refresh_without_proof_refused"), GRANT, DPOP_ERR)),
        ("refresh with another key refused", refused(get(r, "dpop.refresh_other_key_refused"), GRANT, DPOP_ERR)),
        ("refresh with the key", succeeded(get(r, "dpop.refresh_right_key"))),
        ("access token unbound after refresh", equals(get(r, "dpop.refresh_right_key.access_cnf"), False, "cnf")),
        ("token_type Bearer after refresh", equals(get(r, "dpop.refresh_right_key.token_type"), "Bearer", "token_type")),
    ]
    for stage in ("after_refresh", "after_concurrent"):
        checks += [
            (f"{stage}: returned refresh token bound to the key", equals(get(r, f"dpop.{stage}.bound_to_key1"), True, "jkt")),
            (f"{stage}: without proof refused", refused(get(r, f"dpop.{stage}.without_proof"), GRANT, DPOP_ERR)),
            (f"{stage}: another key refused", refused(get(r, f"dpop.{stage}.other_key"), GRANT, DPOP_ERR)),
            (f"{stage}: the key works", succeeded(get(r, f"dpop.{stage}.right_key"))),
            (f"{stage}: access token unbound", equals(get(r, f"dpop.{stage}.right_key.access_cnf"), False, "cnf")),
            (f"{stage}: token_type Bearer", equals(get(r, f"dpop.{stage}.right_key.token_type"), "Bearer", "token_type")),
        ]
    rounds = get(r, "dpop.concurrent_rounds")
    if rounds is MISSING:
        checks.append(("concurrent refreshes", NOT_RUN_PAIR))
    elif not isinstance(rounds, list):
        checks.append(("concurrent refreshes", (ERROR, f"rounds={rounds!r}")))
    else:
        statuses = [st for x in rounds for st in x.get("statuses", [])]
        if any(isinstance(st, int) and st >= 500 for st in statuses):
            checks.append(("concurrent refreshes", (ERROR, f"statuses {statuses}")))
        else:
            n, overlapped = len(rounds), sum(bool(x.get("overlapped")) for x in rounds)
            ok_rounds = sum(x.get("statuses") == [200, 200] and x.get("returned_bound_to_key1") == [True, True]
                            for x in rounds)
            why = (f"{n}/{CONCURRENT_ROUNDS} rounds, {ok_rounds} with both refreshes 200 and both returned tokens "
                   f"bound, {overlapped} with the two client request intervals overlapping")
            good = n == CONCURRENT_ROUNDS and ok_rounds == n and overlapped == n
            checks.append(("concurrent refreshes (client request intervals overlap)", (OK if good else FAIL, why)))
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
    if not isinstance(log, list) or not isinstance(mx, int):
        return [("offline maximum", (ERROR, f"refreshes={log!r} max={mx!r}"))]
    if any(not isinstance(x.get("status"), int) or x["status"] >= 500 for x in log):
        return [("offline maximum", (ERROR, f"{[x.get('status') for x in log]}"))]
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
    first = entry(get(r, "exchange.to_platform"), "exchange to the platform")
    if first[0] != OK:
        return [("standard exchange to the platform", first)]
    return [
        ("exchange to the platform", first),
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
    first = entry(get(r, "exchange.to_platform"), "exchange to the platform")
    if first[0] != OK:
        return [("exchange with LiteLLM's service account off", first)]
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


# (name, judge, record sections the group's results live in)
GROUPS = [
    ("Basic token contract", c_contract, ["contract"]),
    ("Refresh-only DPoP, kept across refreshes (mm-auth)", c_dpop, ["server", "policy_setup", "dpop"]),
    ("Offline token, idle limit, disable", c_offline, ["offline"]),
    ("Offline maximum while in use", c_offline_max, ["offline"]),
    ("Standard token exchange", c_exchange, ["exchange"]),
    ("Exchange with LiteLLM's service account off", c_exchange_sa_off, ["exchange"]),
    ("Device authorization request accepted (not a sign-in)", c_device, ["device"]),
]


def judge(r, fn, sections):
    """A group's checks, with exceptions and missing results resolved by the record's schema."""
    for sec in sections:
        x = r.get(sec, MISSING)
        if x is MISSING or x is None:
            return [(f"{sec} section", (ERROR, "missing from the record"))]
        if isinstance(x, dict) and "exception" in x:
            return [(f"{sec} section", (ERROR, f"exception: {x['exception']}"))]
    try:
        checks = fn(r)
    except Exception as e:                       # a malformed record is no verdict
        return [("judging", (ERROR, repr(e)[:160]))]
    if r.get("schema", 1) >= CURRENT_SCHEMA:     # everything should be there: a gap is a lost result
        checks = [(c, (ERROR, "missing result") if v == NOT_RUN else (v, why)) for c, (v, why) in checks]
    return checks


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


def load(path=SRC):
    recs = {}
    for line in Path(path).read_text().splitlines():
        r = json.loads(line)
        recs[(r["version"], r.get("features_flag"))] = r
    return recs


def main():
    recs = load()
    keys = sorted(recs, key=lambda k: (tuple(int(x) for x in k[0].split(".")), k[1] or ""))
    cols = [f"{v}{' +' + f if f else ''}" for v, f in keys]
    print("| Capability | " + " | ".join(cols) + " |")
    print("|---|" + "---|" * len(cols))
    detail = []
    for name, fn, sections in GROUPS:
        cells = []
        for k in keys:
            r = recs[k]
            if not r.get("started"):
                cells.append("did not start")
                continue
            checks = judge(r, fn, sections)
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
