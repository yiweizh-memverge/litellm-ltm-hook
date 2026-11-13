"""Perturbation tests for summarize.py's verdicts, on real records from kc-matrix.jsonl.

    $PY t2/test_summarize.py        # prints each case; exits non-zero on a wrong verdict

Takes a full schema-2 record (26.7.5) and a schema-1 one (26.5.7), damages a copy in one way per
case, and checks the group verdict: a lost result or a 5xx is ERROR, never "not run" or PASS*.
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import summarize as S  # noqa: E402

RECS = S.load()
NEW = RECS[("26.7.5", None)]
OLD = RECS[("26.5.7", None)]
GROUP = {name: (fn, secs) for name, fn, secs in S.GROUPS}
DPOP, OFF, OFFMAX = ("Refresh-only DPoP, kept across refreshes (mm-auth)", "Offline token, idle limit, disable",
                     "Offline maximum while in use")
EXCH, SAOFF, CONTRACT = ("Standard token exchange", "Exchange with LiteLLM's service account off",
                         "Basic token contract")


def verdict(rec, group):
    fn, secs = GROUP[group]
    return S.verdict(S.judge(rec, fn, secs))


def damaged(rec, edit):
    r = copy.deepcopy(rec)
    edit(r)
    return r


def drop(path):
    def edit(r):
        *head, last = path.split(".")
        x = r
        for k in head:
            x = x[int(k)] if isinstance(x, list) else x[k]
        del x[last]
    return edit


def put(path, value):
    def edit(r):
        *head, last = path.split(".")
        x = r
        for k in head:
            x = x[int(k)] if isinstance(x, list) else x[k]
        x[last] = value
    return edit


CASES = [
    ("schema 2 as recorded", NEW, None, {g: "PASS" for g in GROUP}),
    ("schema 1 as recorded: new checks not run", OLD, None, {DPOP: "PASS*", SAOFF: "PASS*", OFFMAX: "not run"}),
    ("offline group raised", NEW, put("offline", {"exception": "timeout"}), {OFF: "ERROR", OFFMAX: "ERROR"}),
    ("exchange entry answered 500", NEW, put("exchange.to_platform", {"status": 500, "error": {}}),
     {EXCH: "ERROR", SAOFF: "ERROR"}),
    ("exchange entry answered 501 Feature not enabled (Keycloak 22)", NEW,
     put("exchange.to_platform", {"status": 501, "error": {"error": "Feature not enabled"}}), {EXCH: "FAIL"}),
    ("exchange entry answered 501 without an error", NEW, put("exchange.to_platform", {"status": 501}), {EXCH: "ERROR"}),
    ("exchange entry missing in schema 2", NEW, drop("exchange.to_platform"), {EXCH: "ERROR"}),
    ("schema 2 result lost (service account off, other audience)", NEW,
     drop("exchange.service_account_off.other_audience_refused"), {SAOFF: "ERROR"}),
    ("schema 2 result lost (mm-auth audience)", NEW, drop("contract.platform_audience_mm_auth"), {CONTRACT: "ERROR"}),
    ("refusal answered 500", NEW, put("dpop.refresh_other_key_refused", {"status": 500, "error": {}}), {DPOP: "ERROR"}),
    ("refusal with the wrong error code", NEW,
     put("dpop.refresh_other_key_refused", {"status": 400, "error": {"error": "invalid_request"}}), {DPOP: "FAIL"}),
    ("no concurrent rounds", NEW, put("dpop.concurrent_rounds", []), {DPOP: "FAIL"}),
    ("concurrent rounds never overlapped", NEW,
     lambda r: [x.__setitem__("overlapped", False) for x in r["dpop"]["concurrent_rounds"]], {DPOP: "FAIL"}),
    ("one concurrent round short", NEW, lambda r: r["dpop"]["concurrent_rounds"].pop(), {DPOP: "FAIL"}),
    ("token_type DPoP at sign-in", NEW, put("dpop.code_exchange_with_proof.token_type", "DPoP"), {DPOP: "FAIL"}),
    ("token_type DPoP after concurrent refreshes", NEW,
     put("dpop.after_concurrent.right_key.token_type", "DPoP"), {DPOP: "FAIL"}),
    ("offline maximum refresh answered 503", NEW,
     lambda r: r["offline"]["client_offline_max"]["refreshes"][0].update(status=503), {OFFMAX: "ERROR"}),
    ("schema 1 group raised", OLD, put("dpop", {"exception": "boom"}), {DPOP: "ERROR"}),
]


def main():
    bad = 0
    for name, base, edit, want in CASES:
        rec = base if edit is None else damaged(base, edit)
        for group, expected in want.items():
            got = verdict(rec, group)
            ok = got == expected
            bad += not ok
            print(f"{'ok  ' if ok else 'BAD '} {name} · {group}: {got}" + ("" if ok else f" (want {expected})"))
    print(f"\n{len(CASES)} cases, {bad} wrong")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
