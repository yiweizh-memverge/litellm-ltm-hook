"""Turn $T1_ENV/logs/kc-matrix.jsonl into one verdict per capability group and version.

    $PY t2/summarize.py            # markdown table, then the reasons for anything not PASS

The latest record per (version, features flag) wins. PASS needs every check in the group,
refusal cases included; anything else names what failed.
"""
import json
import os
from pathlib import Path

T1_ENV = Path(os.environ.get("T1_ENV", Path.home() / "t1-tokenpath-env"))
SRC = T1_ENV / "logs" / "kc-matrix.jsonl"


def refused(x):
    return isinstance(x, dict) and x.get("status") not in (200, None)


def contract(r):
    c = r.get("contract") or {}
    u, cc = c.get("user_token") or {}, c.get("client_credentials_token") or {}
    bad = []
    if u.get("alg") != "RS256" or u.get("typ") != "Bearer" or u.get("id_token_typ") != "ID":
        bad.append(f"token shape alg={u.get('alg')} typ={u.get('typ')} id_typ={u.get('id_token_typ')}")
    if u.get("client_id_claim"):
        bad.append("user token carries client_id")
    if u.get("lifespan_s") != 900:
        bad.append(f"per-client lifespan not applied ({u.get('lifespan_s')} s)")
    if not cc.get("client_id"):
        bad.append("client-credentials token marked by clientId, not client_id" if cc.get("clientId")
                   else f"client-credentials token unmarked {cc}")
    if not c.get("same_sub_across_clients"):
        bad.append("sub differs across clients")
    if "memmachine-platform" not in (c.get("platform_audience_mm_auth") or []):
        bad.append("mm-auth token lacks the platform audience")
    return bad


def dpop(r):
    d, ps = r.get("dpop") or {}, r.get("policy_setup") or {}
    opts = (r.get("server") or {}).get("dpop_executor_options")
    saved = {k for e in ps.get("saved_executors") or [] for k in (e.get("configuration") or {})}
    bad = []
    if opts is None:
        return ["no dpop-bind-enforcer executor"]
    if "allow-only-refresh-token-binding" not in opts:
        bad.append("executor has no allow-only-refresh-token-binding option")
    elif "allow-only-refresh-token-binding" not in saved:
        bad.append("option not saved")
    if not refused(d.get("code_exchange_without_proof_refused")):
        bad.append("code exchange without proof accepted")
    w = d.get("code_exchange_with_proof") or {}
    if w.get("status") != 200:
        bad.append(f"code exchange with proof failed {w.get('error')}")
    else:
        if w.get("access_cnf") or w.get("token_type") != "Bearer":
            bad.append(f"access token bound at sign-in (token_type {w.get('token_type')})")
        if not w.get("refresh_cnf"):
            bad.append("refresh token not bound")
    if not refused(d.get("refresh_without_proof_refused")):
        bad.append("refresh without proof accepted")
    if not refused(d.get("refresh_other_key_refused")):
        bad.append("refresh with another key accepted")
    rr = d.get("refresh_right_key") or {}
    if rr.get("status") != 200:
        bad.append(f"refresh with the right key failed {rr.get('error')}")
    elif rr.get("access_cnf") or rr.get("token_type") != "Bearer":
        bad.append("access token bound after refresh")
    if d.get("two_refreshes_at_once") != [200, 200]:
        bad.append(f"concurrent refreshes {d.get('two_refreshes_at_once')}")
    if "exception" in d:
        bad.append(d["exception"])
    return bad


def offline(r):
    o = r.get("offline") or {}
    bad = []
    if (o.get("open_webui_offline") or {}).get("refresh_typ") != "Offline":
        bad.append(f"Open WebUI offline token {o.get('open_webui_offline')}")
    if (o.get("mm_auth_offline") or {}).get("refresh_typ") != "Offline":
        bad.append(f"mm-auth offline token {o.get('mm_auth_offline')}")
    lim = o.get("client_offline_idle_limit") or {}
    if lim.get("refresh_within_limit") != 200 or not refused(lim.get("refresh_after_limit")):
        bad.append(f"client offline idle limit not applied {lim}")
    if not refused(o.get("refresh_after_disable_refused")):
        bad.append("disabled person's refresh accepted")
    if "exception" in o:
        bad.append(o["exception"])
    return bad


def exchange(r):
    e = r.get("exchange") or {}
    tp = e.get("to_platform") or {}
    if tp.get("status") != 200:
        return [f"no standard exchange: {tp.get('error') or e.get('exception')}"]
    bad = []
    aud = tp.get("aud")
    if not tp.get("same_sub") or tp.get("azp") != "litellm" or tp.get("act"):
        bad.append(f"exchanged token sub/azp/act {tp}")
    if aud not in ("memmachine-platform", ["memmachine-platform"]):
        bad.append(f"exchanged audience {aud}")
    for k in ("other_audience_refused", "subject_not_naming_litellm_refused", "no_client_secret_refused"):
        if not refused(e.get(k)):
            bad.append(f"{k} accepted")
    return bad


def device(r):
    d = r.get("device") or {}
    return [] if d.get("status") == 200 and d.get("has_device_code") else [f"device request {d.get('error')}"]


GROUPS = [("Basic token contract", contract), ("Refresh-only DPoP (mm-auth)", dpop),
          ("Offline sessions", offline), ("Standard token exchange", exchange), ("Device code (conditional)", device)]


def notes(r):
    o, e = r.get("offline") or {}, r.get("exchange") or {}
    out = []
    if (o.get("old_offline_token_after_reenable") or {}).get("status") == 200:
        out.append("re-enabling a disabled person revives their old offline token")
    s = e.get("own_service_token_as_subject") or {}
    if s.get("status") == 200:
        out.append(f"LiteLLM can exchange its own service token: client_id={s.get('client_id_claim')} "
                   f"clientId={s.get('clientId_claim')} user={s.get('preferred_username')} aud={s.get('aud')}")
    so = e.get("service_account_off") or {}
    if so:
        out.append(f"LiteLLM's service account off: people's exchange {so.get('people_exchange', {}).get('status')}, "
                   f"own token {so.get('own_token_refused', {}).get('status')}")
    tp = e.get("to_platform") or {}
    if tp.get("status") == 200:
        out.append(f"exchanged token lives {tp.get('lifespan_s')} s (LiteLLM's client), subject had "
                   f"{tp.get('subject_lifespan_left_s')} s left")
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
    reasons = []
    for name, fn in GROUPS:
        cells = []
        for k in keys:
            r = recs[k]
            if not r.get("started"):
                cells.append("did not start")
                continue
            bad = fn(r)
            cells.append("PASS" if not bad else "FAIL")
            reasons += [f"- {k[0]}{' +' + k[1] if k[1] else ''}, {name}: {b}" for b in bad]
        print(f"| {name} | " + " | ".join(cells) + " |")
    if reasons:
        print("\nNot PASS:\n" + "\n".join(reasons))
    print("\nNotes:")
    for k in keys:
        for n in notes(recs[k]) if recs[k].get("started") else [recs[k].get("start_error", "")[:300]]:
            print(f"- {k[0]}{' +' + k[1] if k[1] else ''}: {n}")


if __name__ == "__main__":
    main()
