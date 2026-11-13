"""T3: Claude Code + mm-auth, the minimal real loop, against the T1 stack's real Keycloak.

Real Claude Code (`claude -p`, the mm-auth plugin loaded with --plugin-dir, its own config
directory) talks to LiteLLM with a LiteLLM key, and LiteLLM to the fake provider; the plugin's
hooks run t3/plugin/mm_auth.py, which signs in at Keycloak and calls the stand-in gateway.

  L1  sign-in: PKCE code flow back to a random loopback port; an offline refresh token bound to
      mm-auth's key; a bearer access token for mm-auth naming the platform; files only the user reads
  L2  the client refuses an authorization request without PKCE
  L3  a refresh needs the key's proof: with it 200, with none or another key's refused
  H1  one real turn: UserPromptSubmit searches and adds memory as additionalContext; Stop stores
      the prompt and the final answer exactly as Claude Code printed them
  H2  a second session on the same sign-in gets the first session's memory in its model request
  H4  more memory than Claude Code passes on whole (an additionalContext over 10,000 characters
      reaches the model only as a 2,000-character preview of a file): the block stays under it
  H3  the model calls carry no corporate token: none at the provider, none sent to LiteLLM,
      none in LiteLLM's logs or database; the memory calls carried mm-auth's token to the gateway only
  C1  after the access token expires, three sessions at once: one refresh, all served and stored,
      no new sign-in
  F1  not signed in: Claude Code answers, the developer is told to run mm-auth login
  F2  refresh refused (offline session revoked at Keycloak): Claude Code answers, the developer is
      told to sign in again, no retries; a new sign-in brings memory back
  F3  the platform hangs: the prompt waits no longer than the hook's deadline, the next prompt not
      at all (backoff), memory returns after the backoff

    $PY t3/t3_setup.py && $PY t3/t3_check.py      # T1 stack running (t1/svc.sh status)
    $PY t3/t3_check.py --via-litellm              # H1-H3 again, memory calls through LiteLLM's
                                                  # pass-through route (signed in by the run above)
"""
import base64
import concurrent.futures
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "t1"), str(ROOT / "tools")]
import run_checks as rc  # noqa: E402

PLUGIN = ROOT / "t3" / "plugin"
E = rc.T1_ENV / "t3"
HOME, CCONF, PROJ = E / "mm-auth-home", E / "claude-config", E / "project"
os.environ["MM_AUTH_HOME"] = str(HOME)
VIA_LITELLM = "--via-litellm" in sys.argv
if VIA_LITELLM:
    # the plugin's settings for a machine that may reach only LiteLLM: the pass-through route and a
    # shared key LiteLLM restricts to it (made by hand, $T1_ENV/t3/shared-route-key; see REPORT.md)
    cfg = json.loads((PLUGIN / "config.json").read_text())
    cfg.update(api=f"{rc.LITELLM}/memmachine", litellm_key=(E / "shared-route-key").read_text().strip())
    (E / "config-via-litellm.json").write_text(json.dumps(cfg))
    (E / "config-via-litellm.json").chmod(0o600)
    os.environ["MM_AUTH_CONFIG"] = str(E / "config-via-litellm.json")
sys.path.insert(0, str(PLUGIN))
import mm_auth  # noqa: E402

PY = rc.T1_ENV / "litellm-venv" / "bin" / "python"
MM = str(PLUGIN / "mm-auth")
CREDS = HOME / "credentials.json"
seen_tokens = set()                 # mm-auth's access and refresh tokens met in this run


def mm(*args, env=None, timeout=90):
    return subprocess.run([MM, *args], capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **(env or {})})


USER = "carol@corp.example"          # her own memory, apart from T1's alice


def login(email=USER):
    r = mm("login", env={"MM_AUTH_BROWSER": f"{PY} {ROOT / 't3' / 'fake_browser.py'} {email}"})
    note_tokens()
    return r


def note_tokens():
    c = mm_auth._read(CREDS)
    if c:
        seen_tokens.update({c["access_token"], c["refresh_token"]})


def mmlog(t0, event=None):
    out = []
    for line in (HOME / "mm-auth.log").read_text().splitlines() if (HOME / "mm-auth.log").exists() else []:
        e = json.loads(line)
        if e["ts"] >= t0 and (event is None or e["event"] == event):
            out.append(e)
    return out


def claude(prompt):
    """One `claude -p` turn; returns its result, session, hook responses and time taken."""
    key = rc.env_file("litellm.env")["LITELLM_MASTER_KEY"]
    env = {"PATH": f"/usr/bin:/bin:{Path.home() / '.local/bin'}", "HOME": str(Path.home()), "TERM": "dumb",
           "CLAUDE_CONFIG_DIR": str(CCONF), "MM_AUTH_HOME": str(HOME),
           **({"MM_AUTH_CONFIG": os.environ["MM_AUTH_CONFIG"]} if VIA_LITELLM else {}),
           "ANTHROPIC_BASE_URL": rc.LITELLM, "ANTHROPIC_API_KEY": key,
           "ANTHROPIC_MODEL": "fake", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "fake", "ANTHROPIC_SMALL_FAST_MODEL": "fake",
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1"}
    t = time.time()
    p = subprocess.run(["claude", "-p", prompt, "--plugin-dir", str(PLUGIN), "--output-format", "stream-json",
                        "--verbose", "--include-hook-events"], cwd=PROJ, env=env, capture_output=True, text=True,
                       timeout=180)
    out = {"rc": p.returncode, "elapsed": round(time.time() - t, 1), "result": None, "session": None, "hooks": {}}
    for line in p.stdout.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") == "result":
            out["result"], out["session"] = e.get("result"), e.get("session_id")
        if e.get("type") == "system" and e.get("subtype") == "hook_response":
            try:
                o = json.loads(e.get("output") or "{}")
            except json.JSONDecodeError:
                o = {"raw": e.get("output")}
            out["hooks"][e["hook_event"]] = {"outcome": e.get("outcome"), **o}
    time.sleep(1.5)                                    # the store runs in its own process
    return out


def message(turn):
    return (turn["hooks"].get("UserPromptSubmit") or {}).get("systemMessage")


def gw(t0, path=None):
    return [e for e in rc.log_since("gateway.jsonl", t0) if path is None or e.get("path") == path]


def newest_memory():
    tok = mm("token", env={"MM_AUTH_PRINT_TOKEN": "1"}).stdout.strip()
    _, js, _ = rc.http("POST", f"{rc.GATEWAY}/api/v2/memories/search", {"query": "t3"},
                       {"Authorization": f"Bearer {tok}"})
    return ((js or {}).get("episodes") or [None])[-1]


def expire_now():
    """Mark the cached access token expired, as if its lifetime had passed (skips the wait)."""
    c = mm_auth._read(CREDS)
    c["access_exp"] = 0
    mm_auth._write(CREDS, c)


def wait_expiry():
    c = mm_auth._read(CREDS)
    time.sleep(max(c["access_exp"] - mm_auth.SKEW - time.time() + 1, 0))


def thumbprint(key):
    j = json.loads(mm_auth.jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    canon = json.dumps({k: j[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(hashlib.sha256(canon.encode()).digest()).rstrip(b"=").decode()


# ------------------------------------------------------------------ checks

def sign_in():
    import shutil
    shutil.rmtree(HOME, ignore_errors=True)
    t0 = time.time()
    r = login()
    ev = mmlog(t0, "login")
    c = mm_auth._read(CREDS) or {}
    at, rt = mm_auth._claims(c.get("access_token", "e30.e30.")), mm_auth._claims(c.get("refresh_token", "e30.e30."))
    port = urllib.parse.parse_qs(urllib.parse.urlparse(
        next((l.strip() for l in r.stderr.splitlines() if "redirect_uri" in l), "")).query).get("redirect_uri")
    modes = {p.name: oct(p.stat().st_mode & 0o777) for p in [HOME, CREDS, HOME / "dpop-key.pem"]}
    ok = (r.returncode == 0 and ev and ev[0]["outcome"] == "signed-in" and rt.get("typ") == "Offline"
          and (rt.get("cnf") or {}).get("jkt") == thumbprint(mm_auth._key()) and at.get("typ") == "Bearer"
          and "cnf" not in at and at.get("azp") == "mm-auth" and "memmachine-platform" in (at.get("aud") or [])
          and modes == {"mm-auth-home": "0o700", "credentials.json": "0o600", "dpop-key.pem": "0o600"})
    rc.record("L1", "Sign-in: PKCE to a loopback port, offline refresh token bound to the key, bearer access token",
              ok, f"redirect {port}; refresh typ {rt.get('typ')}, bound to the installation key "
                  f"{(rt.get('cnf') or {}).get('jkt') == thumbprint(mm_auth._key())}; access typ {at.get('typ')}, "
                  f"cnf {'cnf' in at}, azp {at.get('azp')}, aud {at.get('aud')}; modes {modes}; {r.stdout.strip()}")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a):
            return None
    q = urllib.parse.urlencode({"client_id": "mm-auth", "response_type": "code", "scope": "openid",
                                "redirect_uri": "http://127.0.0.1:1/callback", "state": "x"})
    try:
        urllib.request.build_opener(NoRedirect).open(f"{rc.KC}/realms/{rc.REALM}/protocol/openid-connect/auth?{q}")
        loc = "(no redirect)"
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") or f"HTTP {e.code}"
    err = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    rc.record("L2", "An authorization request without PKCE is refused", err.get("error") == ["invalid_request"],
              f"error {err.get('error')}: {err.get('error_description')}")


def refresh_needs_key():
    c = mm_auth._read(CREDS)
    url = mm_auth.discovery()["token_endpoint"]
    form = {"grant_type": "refresh_token", "refresh_token": c["refresh_token"], "client_id": "mm-auth"}
    other = mm_auth.ec.generate_private_key(mm_auth.ec.SECP256R1())
    jwk = json.loads(mm_auth.jwt.algorithms.ECAlgorithm.to_jwk(other.public_key()))
    other_proof = mm_auth.jwt.encode({"jti": str(uuid.uuid4()), "htm": "POST", "htu": url, "iat": int(time.time())},
                                     other, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": jwk})
    s_none, j_none = mm_auth._http("POST", url, form=form)
    s_other, j_other = mm_auth._http("POST", url, form=form, headers={"DPoP": other_proof})
    t0 = time.time()
    expire_now()
    r = mm("token", env={"MM_AUTH_PRINT_TOKEN": "1"})
    note_tokens()
    ev = mmlog(t0, "refresh")
    rc.record("L3", "A refresh needs the key's proof: with it 200, with none or another key's refused",
              s_none == 400 and s_other == 400 and r.returncode == 0 and [e["status"] for e in ev] == [200],
              f"no proof: {s_none} {(j_none or {}).get('error')}; another key: {s_other} {(j_other or {}).get('error')}; "
              f"mm-auth with its key: {[e['status'] for e in ev]}")


def real_turns(tag):
    t0 = time.time()
    p1 = f"t3 fact {tag}: my codename is heron-{tag}"
    a = claude(p1)
    note_tokens()
    s1 = [e for e in mmlog(t0, "search")]
    st = [e for e in mmlog(t0, "store")]
    newest = newest_memory()
    want = f"user: {p1} | assistant: {a['result']}"
    rc.record("H1", "A real turn: search adds memory as additionalContext; Stop stores the prompt and final answer",
              a["rc"] == 0 and bool(a["result"]) and s1 and s1[0]["outcome"] == "served"
              # additionalContext whenever the search returned memory (none for a new person)
              and ("additionalContext" in (a["hooks"].get("UserPromptSubmit") or {}).get("hookSpecificOutput", {}))
              == (s1[0]["returned"] > 0)
              and [e["outcome"] for e in st] == ["stored"] and st[0]["answer_from"] == "hook input" and newest == want,
              f"claude rc {a['rc']} in {a['elapsed']} s; search {[(e['outcome'], e['returned'], e['ms']) for e in s1]}; "
              f"store {[(e['outcome'], e['answer_from']) for e in st]}; stored = prompt + printed answer: {newest == want}")

    t1 = time.time()
    b = claude("what is my codename?")
    prov = [e for e in rc.log_since("provider.jsonl", t1) if e.get("path", "").endswith("/chat/completions")]
    in_request = any(f"heron-{tag}" in json.dumps(e.get("body")) for e in prov)
    rc.record("H2", "A second session on the same sign-in: the first session's memory reaches its model request",
              b["rc"] == 0 and b["session"] != a["session"] and in_request and f"heron-{tag}" in (b["result"] or "")
              and not mmlog(t0, "login"),
              f"sessions {a['session'][:8]} then {b['session'][:8]}; memory in the model request: {in_request}; "
              f"in the answer: {f'heron-{tag}' in (b['result'] or '')}; sign-ins since: {len(mmlog(t0, 'login'))}")

    # H4: more memory than Claude Code passes on as it is (10,000 characters)
    tok = mm("token", env={"MM_AUTH_PRINT_TOKEN": "1"}).stdout.strip()
    h = {"Authorization": f"Bearer {tok}"}
    for i in range(5):                          # five, each cut to 2,000 characters: over the budget
        _, js, _ = rc.http("POST", f"{rc.GATEWAY}/api/v2/memories/search", {"query": "seed"}, h)
        rc.http("POST", f"{rc.GATEWAY}/api/v2/memories", {"binding": js["binding"], "messages": [
            {"role": "user", "content": f"t3 long {tag} {i}: " + "lorem ipsum " * 500}]}, h)
    t2 = time.time()
    c = claude("what was the last long note?")
    prov = [e for e in rc.log_since("provider.jsonl", t2) if e.get("path", "").endswith("/chat/completions")]
    body = json.dumps([e.get("body") for e in prov])
    s = (mmlog(t2, "search") or [{}])[0]
    rc.record("H4", "More memory than Claude Code passes on whole: the block stays under its limit, newest kept",
              c["rc"] == 0 and "persisted-output" not in body and f"t3 long {tag} 4" in body
              and 0 < s.get("context_chars", 10 ** 6) <= 10000 and s.get("added", 0) < s.get("returned", 0),
              f"search returned {s.get('returned')}, added {s.get('added')} in {s.get('context_chars')} characters; "
              f"newest in the model request: {f't3 long {tag} 4' in body}; Claude Code's file preview used: "
              f"{'persisted-output' in body}")
    return t0


def no_token_in_model_calls(t0):
    sigs = {t.split(".")[2].encode() for t in seen_tokens}
    prov = [e for e in rc.log_since("provider.jsonl", t0) if e.get("path", "").endswith("/chat/completions")]
    prov_raw = json.dumps(prov).encode()
    probe = [e for e in rc.log_since("probe.jsonl", t0) if e.get("call_type") == "anthropic_messages"]
    files = {}
    for name in ("provider.jsonl", "probe.jsonl", "litellm.out", "fake_provider.out"):
        data = (rc.LOGS / name).read_bytes() if (rc.LOGS / name).exists() else b""
        files[name] = sum(data.count(s) for s in sigs)
    dump = subprocess.run(["docker", "exec", "t1-tokenpath-postgres-1", "pg_dump", "-U", "litellm", "-p", "15432",
                           "-h", "127.0.0.1", "--data-only", "litellm"], capture_output=True).stdout
    files["LiteLLM database"] = sum(dump.count(s) for s in sigs)
    to_gw = {(str((e.get("claims") or {}).get("azp")), json.dumps((e.get("claims") or {}).get("aud")))
             for e in gw(t0) if e.get("path", "").startswith("/api/v2/memories")}
    auth_hdr = {(e.get("headers") or {}).get("authorization") for e in prov}
    rc.record("H3", "Model calls carry no corporate token; the memory calls carried mm-auth's to the gateway only",
              bool(prov) and not rc.JWT_RE.findall(prov_raw) and auth_hdr == {"Bearer fake-provider-key"}
              and probe and all("authorization" not in e.get("header_names", []) for e in probe)
              and not any(files.values()) and to_gw and all(a == "mm-auth" for a, _ in to_gw),
              f"{len(prov)} provider requests, JWTs in them {len(rc.JWT_RE.findall(prov_raw))}, auth {auth_hdr}; "
              f"{len(probe)} Claude Code requests at LiteLLM, any Authorization header: "
              f"{any('authorization' in e.get('header_names', []) for e in probe)}; this run's mm-auth tokens found in "
              f"{files}; gateway saw (azp, aud) {sorted(to_gw)}")


def concurrent_sessions(tag):
    wait_expiry()
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(3) as ex:
        turns = list(ex.map(claude, [f"t3 concurrent {tag} {i}" for i in range(3)]))
    time.sleep(2)
    note_tokens()
    ref = mmlog(t0, "refresh")
    shared = mmlog(t0, "refresh-shared")
    srch = mmlog(t0, "search")
    st = mmlog(t0, "store")
    rc.record("C1", "Access token expired, three sessions at once: one refresh, all served and stored, no sign-in",
              all(t["rc"] == 0 and t["result"] for t in turns) and [e["status"] for e in ref] == [200]
              and [e["outcome"] for e in srch] == ["served"] * 3 and [e["outcome"] for e in st] == ["stored"] * 3
              and not mmlog(t0, "login") and len({t["session"] for t in turns}) == 3,
              f"refreshes {[e['status'] for e in ref]}, waited for another's {len(shared)}; searches "
              f"{[e['outcome'] for e in srch]}; stores {[e['outcome'] for e in st]}; "
              f"claude {[(t['rc'], t['elapsed']) for t in turns]}")


def not_signed_in(tag):
    aside = CREDS.with_name("credentials.aside")
    CREDS.rename(aside)
    try:
        t0 = time.time()
        a = claude(f"t3 signed out {tag}")
        msg = message(a)
        rc.record("F1", "Not signed in: Claude Code answers, the developer is told to run mm-auth login",
                  a["rc"] == 0 and bool(a["result"]) and msg and "mm-auth login" in msg and not gw(t0)
                  and [e["reason"] for e in mmlog(t0, "store-skip")] == ["no binding"],
                  f"claude rc {a['rc']} in {a['elapsed']} s; shown: {msg!r}; gateway requests {len(gw(t0))}")
    finally:
        aside.rename(CREDS)


def refresh_refused(tag):
    t = rc.kc_admin()
    h = {"Authorization": f"Bearer {t}"}
    uid = rc.http("GET", f"{rc.KC}/admin/realms/{rc.REALM}/users?email={USER}&exact=true", headers=h)[1][0]["id"]
    s_rev = rc.http("DELETE", f"{rc.KC}/admin/realms/{rc.REALM}/users/{uid}/consents/mm-auth", headers=h)[0]
    expire_now()
    t0 = time.time()
    a = claude(f"t3 revoked {tag} one")
    b = claude(f"t3 revoked {tag} two")
    ref = mmlog(t0, "refresh")
    msgs = [message(a), message(b)]
    t1 = time.time()
    reached = len(gw(t0, "/api/v2/memories/search"))
    r = login()
    c = claude(f"t3 revoked {tag} after sign-in")
    back = mmlog(t1, "search")
    rc.record("F2", "Refresh refused (offline session revoked): answers go on, told to sign in again, no retries; "
              "a new sign-in brings memory back",
              s_rev == 204 and a["rc"] == b["rc"] == 0 and a["result"] and b["result"]
              and all(m and "mm-auth login" in m for m in msgs) and [(e["status"], e.get("error")) for e in ref]
              == [(400, "invalid_grant")] and reached == 0
              and r.returncode == 0 and back and back[-1]["outcome"] == "served" and c["rc"] == 0,
              f"revoke HTTP {s_rev}; refresh attempts {[(e['status'], e.get('error')) for e in ref]}; "
              f"gateway searches while refused {reached}; shown: {msgs[0]!r}; "
              f"after sign-in: {[e['outcome'] for e in back]}")


def platform_hangs(tag):
    pid = int((rc.T1_ENV / "run" / "gateway.pid").read_text())
    deadline = mm_auth.CONFIG["deadline_s"]
    os.kill(pid, signal.SIGSTOP)
    try:
        t0 = time.time()
        a = claude(f"t3 hang {tag} one")
        b = claude(f"t3 hang {tag} two")
        s = mmlog(t0, "search")
    finally:
        os.kill(pid, signal.SIGCONT)
    first, second = (s + [{}, {}])[:2]
    rc.record("F3", "Platform hangs: the prompt waits at most the deadline, the next not at all, both answered",
              a["rc"] == b["rc"] == 0 and a["result"] and b["result"]
              and first.get("outcome") == second.get("outcome") == "unavailable"
              and first.get("ms", 1e9) <= deadline * 1000 + 500 and second.get("ms", 1e9) < 500
              and message(a) and "retrying automatically" in message(a),
              f"search waited {first.get('ms')} ms then {second.get('ms')} ms (deadline {deadline} s); "
              f"claude {a['elapsed']} s, {b['elapsed']} s; shown: {message(a)!r} / {message(b)!r}")
    h = mm_auth._read(mm_auth.HEALTH, {}).get("api") or {}
    time.sleep(max(h.get("until", 0) - time.time() + 0.5, 0))
    t1 = time.time()
    c = claude(f"t3 hang {tag} recovered")
    s2 = mmlog(t1, "search")
    rc.record("F3", "After the backoff, one probe finds the platform back and memory returns",
              c["rc"] == 0 and [e["outcome"] for e in s2] == ["served"] and mmlog(t1, "backoff-cleared")
              and [e["outcome"] for e in mmlog(t1, "store")] == ["stored"],
              f"search {[e['outcome'] for e in s2]}; store {[e['outcome'] for e in mmlog(t1, 'store')]}")


def via_litellm():
    """P: the same turns with mm-auth's memory calls through LiteLLM's pass-through route."""
    tag = uuid.uuid4().hex[:6]
    note_tokens()
    t0 = real_turns(tag)
    no_token_in_model_calls(t0)
    for r in rc.results:
        r["id"] = "P-" + r["id"]
    seen = [e for e in rc.log_since("probe.jsonl", t0) if e.get("call_type") == "pass_through_endpoint"]
    rc.record("P", "The memory calls went through LiteLLM's pass-through route", bool(seen) or None,
              f"pass-through calls LiteLLM's callbacks saw: {len(seen)}")
    rc.save("t3-via-litellm")
    print("\n" + "\n".join(f"{r['status']:4} {r['id']:5} {r['check']}" for r in rc.results))


def main():
    if VIA_LITELLM:
        return via_litellm()
    rc.TESTER_SECRET = rc.ensure_tester()
    CCONF.mkdir(parents=True, exist_ok=True)
    PROJ.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:6]
    sign_in()
    refresh_needs_key()
    t0 = real_turns(tag)
    concurrent_sessions(tag)
    no_token_in_model_calls(t0)
    not_signed_in(tag)
    refresh_refused(tag)
    platform_hangs(tag)
    rc.save("t3")
    print("\n" + "\n".join(f"{r['status']:4} {r['id']:3} {r['check']}" for r in rc.results))


if __name__ == "__main__":
    main()
