"""T1 checks A1-A12 for approach A's user-token path. See t1/README.md.

    PY=~/t1-tokenpath-env/litellm-venv/bin/python
    $PY t1/run_checks.py core       # A1-A6, A10-A12 with the running stack
    $PY t1/run_checks.py leaks      # A8: search every log and LiteLLM's database for tokens
    $PY t1/run_checks.py refresh    # A7: Open WebUI refreshes per prompt at 300 s vs 900 s
    $PY t1/run_checks.py forward    # A9: restarts LiteLLM with forward_llm_provider_auth_headers on, then off
    $PY t1/run_checks.py debug      # A8b: restarts LiteLLM with --detailed_debug, then without
    $PY t1/run_checks.py all        # every one of the above, in that order

Results print as a table and are saved to $T1_ENV/logs/results-<open-webui version>-<check>.json.

Test-only Keycloak client: "t1-tester" (confidential, password and client-credentials
grants, 10-second access tokens) mints tokens for the negative and tool-round checks.
The gateway is started with it allowed (t1/svc.sh), so its tokens show what an
allowed client's ID, expired or client-credentials token gets.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

T1 = Path(__file__).resolve().parent
sys.path.insert(0, str(T1.parent / "tools"))
from sso_browser import sso_login  # noqa: E402

T1_ENV = Path(os.environ.get("T1_ENV", Path.home() / "t1-tokenpath-env"))
LOGS = T1_ENV / "logs"
KC = "http://127.0.0.1:18180"
REALM = "corp"
OWUI = "http://127.0.0.1:18087"
LITELLM = "http://127.0.0.1:18011"
GATEWAY = "http://127.0.0.1:18100"
MODEL = "memmachine-fake"
JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")

results = []
minted = []          # every token this run minted, for the leak search


def env_file(name):
    out = {}
    for line in (T1_ENV / name).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'")
    return out


def accounts():
    return dict(line.split(None, 1) for line in (T1_ENV / "accounts.txt").read_text().splitlines() if line.strip())


def record(cid, name, ok, detail):
    status = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    results.append({"id": cid, "check": name, "status": status, "detail": detail})
    print(f"{status:4}  {cid:5} {name}\n            {detail}")


# ---------------------------------------------------------------- HTTP helpers

def http(method, url, body=None, headers=None, form=None, opener=None, timeout=60):
    data, h = None, dict(headers or {})
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        r = (opener.open if opener else urllib.request.urlopen)(req, timeout=timeout)
        raw, status, rh = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, status, rh = e.read(), e.code, e.headers
    try:
        js = json.loads(raw) if raw else None
    except ValueError:
        js = raw.decode(errors="replace")[:300]
    return status, js, rh


def kc_admin():
    c = env_file("compose.env")
    _, js, _ = http("POST", f"{KC}/realms/master/protocol/openid-connect/token", form={
        "grant_type": "password", "client_id": "admin-cli",
        "username": c["KC_ADMIN"], "password": c["KC_ADMIN_PASSWORD"]})
    return js["access_token"]


def kc_client(t, client_id):
    _, found, _ = http("GET", f"{KC}/admin/realms/{REALM}/clients?clientId={client_id}",
                       headers={"Authorization": f"Bearer {t}"})
    return found[0] if found else None


def ensure_tester():
    t = kc_admin()
    rep = {"clientId": "t1-tester", "enabled": True, "protocol": "openid-connect",
           "publicClient": False, "standardFlowEnabled": False, "directAccessGrantsEnabled": True,
           "serviceAccountsEnabled": True, "attributes": {"access.token.lifespan": "10"}}
    c = kc_client(t, "t1-tester")
    if c is None:
        http("POST", f"{KC}/admin/realms/{REALM}/clients", rep, {"Authorization": f"Bearer {t}"})
        c = kc_client(t, "t1-tester")
    _, sec, _ = http("GET", f"{KC}/admin/realms/{REALM}/clients/{c['id']}/client-secret",
                     headers={"Authorization": f"Bearer {t}"})
    return sec["value"]


def set_owui_lifespan(seconds):
    """Access-token lifespan of Open WebUI's client; None = the realm default (300 s)."""
    t = kc_admin()
    c = kc_client(t, "open-webui")
    attrs = dict(c.get("attributes") or {})
    if seconds is None:
        # An empty value restores the realm's; leaving the key out of the update keeps the old one.
        attrs["access.token.lifespan"] = ""
    else:
        attrs["access.token.lifespan"] = str(seconds)
    c["attributes"] = attrs
    s, _, _ = http("PUT", f"{KC}/admin/realms/{REALM}/clients/{c['id']}", c, {"Authorization": f"Bearer {t}"})
    assert s == 204, s


TESTER_SECRET = None


def mint(email=None, grant="password"):
    """Tokens from t1-tester: a person's (password grant) or the client's own (client_credentials)."""
    form = {"grant_type": grant, "client_id": "t1-tester", "client_secret": TESTER_SECRET}
    if grant == "password":
        form.update(username=email, password=accounts()[email], scope="openid")
    _, js, _ = http("POST", f"{KC}/realms/{REALM}/protocol/openid-connect/token", form=form)
    for k in ("access_token", "id_token"):
        if js.get(k):
            minted.append(js[k])
    return js


def claims(token):
    p = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


def owui_session(email):
    opener, _ = sso_login(f"{OWUI}/oauth/oidc/login", email, accounts()[email])
    tok = next(c.value for h in opener.handlers if getattr(h, "cookiejar", None) is not None
               for c in h.cookiejar if c.name == "token")
    return opener, tok


def owui_chat(session, text, model=MODEL, cookies=True):
    """One prompt through Open WebUI's API, as its chat page sends it (message IDs included)."""
    opener, tok = session
    mid, umid = str(uuid.uuid4()), str(uuid.uuid4())
    body = {"model": model, "stream": False, "chat_id": f"local:{uuid.uuid4()}", "id": mid,
            "messages": [{"role": "user", "content": text}],
            "user_message": {"id": umid, "role": "user", "content": text}}
    s, js, _ = http("POST", f"{OWUI}/api/chat/completions", body, {"Authorization": f"Bearer {tok}"},
                    opener=opener if cookies else None, timeout=120)
    reply = js["choices"][0]["message"]["content"] if s == 200 else f"HTTP {s}: {js}"
    return mid, reply


def litellm_chat(user_token, messages, headers=None, tools=None, key=True):
    h = dict(headers or {})
    if key:
        h["x-litellm-api-key"] = env_file("keys.env")["OPENWEBUI_APP_KEY"]
    if user_token:
        h["Authorization"] = f"Bearer {user_token}"
    body = {"model": MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
    return http("POST", f"{LITELLM}/v1/chat/completions", body, h, timeout=120)


def gateway_search(token):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    s, js, _ = http("POST", f"{GATEWAY}/api/v2/memories/search", {"query": "t1"}, h)
    return s, (js or {}).get("reason") if isinstance(js, dict) else js


def log_since(name, t0):
    out = []
    p = LOGS / name
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("ts", 0) >= t0:
                out.append(e)
    return out


def injected(reply):
    """How many memory items the fake provider says it was handed."""
    m = re.search(r"\[memory injected: (\d+) item", reply or "")
    return int(m.group(1)) if m else 0


def owui_version():
    _, js, _ = http("GET", f"{OWUI}/api/version")
    return "v" + js["version"] if isinstance(js, dict) else "unknown"


def svc(*args, env=None):
    subprocess.run([str(T1 / "svc.sh"), *args], check=True, env={**os.environ, **(env or {})})


def save(label):
    path = LOGS / f"results-{owui_version()}-{label}.json"
    path.write_text(json.dumps(results, indent=1))
    print(f"\nsaved {path}")


# ---------------------------------------------------------------- checks

def core():
    alice, bob = owui_session("alice@corp.example"), owui_session("bob@corp.example")
    ver = owui_version()

    # A1: memory round trip for one person, through Open WebUI
    tag = uuid.uuid4().hex[:6]
    t0 = time.time()
    m1, _ = owui_chat(alice, f"t1 fact {tag}: my codename is falcon-{tag}")
    time.sleep(1.5)
    m2, reply = owui_chat(alice, "what is my codename?")
    time.sleep(1)
    ev = log_since("hook.jsonl", t0)
    s1 = [e for e in ev if e.get("turn") == m1]
    ok = (f"falcon-{tag}" in reply and [e["event"] for e in s1] == ["search", "store"]
          and all(e.get("status") == 200 for e in s1))
    record("A1", "Memory round trip through Open WebUI (search + store per turn)", ok,
           f"turn 1 events {[e['event'] + ':' + str(e.get('status')) for e in s1]}; "
           f"turn 2 reply carries the fact: {f'falcon-{tag}' in reply}")

    # A2: isolation
    _, breply = owui_chat(bob, "what is my codename?")
    st = http("GET", f"{GATEWAY}/t1/state")[1]["accounts"]
    users = {a["username"]: a["id"] for a in st.values()}
    record("A2", "Isolation: bob does not get alice's memory", f"falcon-{tag}" not in breply
           and users.get("alice@corp.example") != users.get("bob@corp.example"),
           f"bob's reply carries alice's fact: {f'falcon-{tag}' in breply}; separate accounts: "
           f"{users.get('alice@corp.example') != users.get('bob@corp.example')}")

    # A3: the gateway refuses every token it should
    fresh = mint("alice@corp.example")
    acc, idt = fresh["access_token"], fresh["id_token"]
    head, payload, sig = acc.split(".")
    tampered = f"{head}.{payload}.{sig[:10]}{'A' if sig[10] != 'A' else 'B'}{sig[11:]}"
    hs = (base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
          + "." + payload + ".c2lnbmF0dXJl")
    master = kc_admin()
    minted.append(master)
    cc_tester = mint(grant="client_credentials")["access_token"]
    plat = env_file("keycloak-clients.env")["PLATFORM_OIDC_CLIENT_SECRET"]
    cc_plat = http("POST", f"{KC}/realms/{REALM}/protocol/openid-connect/token", form={
        "grant_type": "client_credentials", "client_id": "memmachine-platform", "client_secret": plat})[1]["access_token"]
    minted.append(cc_plat)
    cases = [
        ("no token", None, "no-bearer"),
        ("not a JWT", "abc.def.ghi", "invalid:DecodeError"),
        ("signature altered", tampered, "invalid:InvalidSignatureError"),
        ("HS256 forgery", hs, "alg-not-rs256"),
        ("another realm (master)", master, "wrong-issuer"),
        ("ID token, allowed client", idt, "not-access-token:ID"),
        ("client not named for the memories API", cc_plat, "client-not-allowed:memmachine-platform"),
        ("client-credentials token, allowed client", cc_tester, "client-credentials-token"),
    ]
    for name, tok, want in cases:
        s, reason = gateway_search(tok)
        record("A3", f"Gateway refuses: {name}", s == 401 and reason == want, f"HTTP {s}, reason {reason}")
    s, _ = gateway_search(acc)
    st = http("GET", f"{GATEWAY}/t1/state")[1]["accounts"]
    same = claims(acc)["sub"] in st and sum(1 for a in st.values() if a["username"] == "alice@corp.example") == 1
    record("A3", "Gateway serves alice's token from another allowed client, as the same account", s == 200 and same,
           f"HTTP {s}; one account for alice's sub across clients: {same}")
    t0 = time.time()
    s, js, _ = litellm_chat("abc.def.ghi", [{"role": "user", "content": "bad token through LiteLLM"}])
    time.sleep(1)
    srch = [e for e in log_since("hook.jsonl", t0) if e["event"] == "search"]
    record("A3", "A refused token through LiteLLM: no memory, chat still answers", s == 200 and srch
           and srch[-1].get("status") == 401, f"chat HTTP {s}; hook search status {srch[-1].get('status') if srch else None}")
    print("            (waiting 45 s for alice's 10-second token to expire past the 30-second leeway)")
    time.sleep(45)
    s, reason = gateway_search(acc)
    record("A3", "Gateway refuses: expired token", s == 401 and reason == "expired", f"HTTP {s}, reason {reason}")

    # A4: requests the hook must leave alone
    tok = mint("alice@corp.example")["access_token"]
    for name, hdrs, utok, want in [
        ("Open WebUI task (title_generation)", {"x-openwebui-task": "title_generation"}, tok, ("skip-task", None)),
        ("unresolved {{MODEL_ID}} (Open WebUI < 0.12.2)", {"x-openwebui-model-id": "{{MODEL_ID}}"}, tok,
         ("no-memory", "unresolved placeholder")),
        ("no user token", {}, None, ("no-memory", "no user token")),
    ]:
        t0 = time.time()
        s, _, _ = litellm_chat(utok, [{"role": "user", "content": f"a4 {name}"}], hdrs)
        time.sleep(1)
        ev = log_since("hook.jsonl", t0)
        got = [(e["event"], e.get("reason")) for e in ev]
        record("A4", f"No memory call for: {name}", s == 200 and got == [want], f"chat HTTP {s}; hook {got}")

    # A5 + A6: a turn with a tool round; Open WebUI sends each request with a fresh token
    tok_a = mint("alice@corp.example")["access_token"]
    tok_b = mint("alice@corp.example")["access_token"]
    mid = str(uuid.uuid4())
    hdrs = {"x-openwebui-message-id": mid, "x-openwebui-task": "", "x-openwebui-model-id": MODEL}
    tools = [{"type": "function", "function": {"name": "lookup", "description": "t1 tool",
                                               "parameters": {"type": "object", "properties": {}}}}]
    user = {"role": "user", "content": "[tool] look something up"}
    t0 = time.time()
    s1, r1, _ = litellm_chat(tok_a, [user], hdrs, tools)
    call = r1["choices"][0]["message"]["tool_calls"][0]
    s2, r2, _ = litellm_chat(tok_b, [user, {"role": "assistant", "content": None, "tool_calls": [call]},
                                      {"role": "tool", "tool_call_id": call["id"], "content": "42"}], hdrs, tools)
    time.sleep(1.5)
    ev = [e["event"] for e in log_since("hook.jsonl", t0) if e.get("turn") == mid]
    stores = [e for e in log_since("hook.jsonl", t0) if e.get("turn") == mid and e["event"] == "store"]
    record("A5", "Tool round: one search, one store per turn", s1 == s2 == 200 and ev.count("search") == 1
           and ev.count("store") == 1 and "tool-round" in ev, f"hook events for the turn: {ev}")
    record("A6", "Token changed mid-turn: store uses the latest token and is accepted",
           "token-changed" in ev and stores and stores[0].get("status") == 200
           and stores[0].get("token") == hashlib.sha256(tok_b.encode()).hexdigest()[:12],
           f"store status {stores[0].get('status') if stores else None}, with the second token: "
           f"{bool(stores) and stores[0].get('token') == hashlib.sha256(tok_b.encode()).hexdigest()[:12]}")

    # A10: LiteLLM authenticates Open WebUI's key from x-litellm-api-key, never the user's token
    s, _, _ = litellm_chat(tok, [{"role": "user", "content": "a10"}], key=False)
    key_hash = hashlib.sha256(env_file("keys.env")["OPENWEBUI_APP_KEY"].encode()).hexdigest()
    q = subprocess.run(["docker", "exec", "t1-tokenpath-postgres-1", "psql", "-U", "litellm", "-d", "litellm",
                        "-p", "15432", "-h", "127.0.0.1", "-At", "-c",
                        'select api_key from "LiteLLM_SpendLogs" order by "startTime" desc limit 5'],
                       capture_output=True, text=True).stdout.split()
    record("A10", "LiteLLM: user token alone is refused; spend is booked to Open WebUI's key",
           s == 401 and q and all(k == key_hash for k in q), f"user token without key: HTTP {s}; "
           f"last {len(q)} spend rows on Open WebUI's key: {bool(q) and all(k == key_hash for k in q)}")

    # A11: a call to Open WebUI's API without the browser's cookies
    t0 = time.time()
    _, reply = owui_chat(alice, "a11 cookie-less call", cookies=False)
    time.sleep(1)
    pr = [e for e in log_since("probe.jsonl", t0) if e.get("model") == MODEL]
    has = bool(pr and pr[-1].get("token_sha256_12"))
    record("A11", f"Cookie-less Open WebUI API call ({ver})", None,
           f"user token sent to LiteLLM: {has}; memory items injected: {injected(reply)}")

    # A12: a workspace model; {{MODEL_ID}} names it
    _, tok_alice = alice
    http("POST", f"{OWUI}/api/v1/models/create",
         {"id": "t1-team-model", "base_model_id": MODEL, "name": "T1 team model",
          "meta": {"description": "T1 workspace model"}, "params": {}, "access_control": None},
         {"Authorization": f"Bearer {tok_alice}"})
    # Open WebUI chats only with models in its cached list, which /api/models refreshes.
    http("GET", f"{OWUI}/api/models", headers={"Authorization": f"Bearer {tok_alice}"}, opener=alice[0])
    t0 = time.time()
    _, reply = owui_chat(alice, "a12 workspace model", model="t1-team-model")
    time.sleep(1)
    pr = [e for e in log_since("probe.jsonl", t0) if e.get("model") == MODEL]
    hk = [(e["event"], e.get("reason")) for e in log_since("hook.jsonl", t0)]
    mid_hdr = pr[-1]["owui"].get("x-openwebui-model-id") if pr else None
    record("A12", f"Workspace model ({ver}): x-openwebui-model-id", None,
           f"model sent to LiteLLM {pr[-1]['model'] if pr else None}; header {mid_hdr!r}; hook {hk}")
    save("core")


def leaks():
    t0 = time.time()
    found = {}
    sigs = {t.split(".")[2].encode() for t in minted if t.count(".") == 2}
    for name in ["provider.jsonl", "litellm.out", "gateway.out", "gateway.jsonl", "hook.jsonl", "probe.jsonl",
                 "fake_provider.out"]:
        p = LOGS / name
        data = p.read_bytes() if p.exists() else b""
        found[name] = (len(JWT_RE.findall(data)), sum(data.count(s) for s in sigs))
    dump = subprocess.run(["docker", "exec", "t1-tokenpath-postgres-1", "pg_dump", "-U", "litellm", "-p", "15432",
                           "-h", "127.0.0.1", "--data-only", "litellm"], capture_output=True).stdout
    found["LiteLLM database (pg_dump)"] = (len(JWT_RE.findall(dump)), sum(dump.count(s) for s in sigs))
    for where, (jwts, mine) in found.items():
        record("A8", f"No token in {where}", jwts == 0 and mine == 0,
               f"JWT-shaped strings: {jwts}; this run's tokens: {mine}")
    ow = (LOGS / "openwebui.out").read_bytes() if (LOGS / "openwebui.out").exists() else b""
    record("A8", "Open WebUI's own log (outside the hook's path, for reference)", None,
           f"JWT-shaped strings: {len(JWT_RE.findall(ow))}")
    save("leaks")


def refresh():
    results_local = []
    for lifespan in (None, 900):
        set_owui_lifespan(lifespan)
        sess = owui_session("carol@corp.example")      # a fresh session gets the new lifespan
        log = LOGS / "openwebui.out"
        start = log.stat().st_size
        n = 5
        for i in range(n):
            owui_chat(sess, f"a7 prompt {i}")
            time.sleep(1)
        with open(log, "rb") as f:
            f.seek(start)
            refreshes = f.read().count(b"Successfully refreshed token")
        label = "300 s (realm default)" if lifespan is None else f"{lifespan} s"
        record("A7", f"Open WebUI token refreshes, access token {label}", None,
               f"{refreshes} refreshes for {n} prompts ({refreshes / n:.1f} per prompt)")
        results_local.append((label, refreshes))
    set_owui_lifespan(None)
    save("refresh")


def forward():
    svc("restart", "litellm", env={"LITELLM_CONFIG": "litellm-config-forward.yaml"})
    try:
        tok = mint("alice@corp.example")["access_token"]
        t0 = time.time()
        s, _, _ = litellm_chat(tok, [{"role": "user", "content": "a9 forward on"}])
        time.sleep(1.5)
        hk = [(e["event"], e.get("reason")) for e in log_since("hook.jsonl", t0)]
        prov = log_since("provider.jsonl", t0)
        sent = [e["headers"].get("authorization", "") for e in prov]
        leaked = any(tok in a for a in sent)
        record("A9", "forward_llm_provider_auth_headers on: the hook turns memory off", s == 200 and
               ("memory-off", "forward_llm_provider_auth_headers is on") in hk, f"chat HTTP {s}; hook {hk}")
        record("A9", "forward_llm_provider_auth_headers on: what the provider receives", None,
               f"user's token reached the provider: {leaked}")
    finally:
        svc("restart", "litellm")
    save("forward")


def debug():
    svc("restart", "litellm", env={"LITELLM_EXTRA_ARGS": "--detailed_debug"})
    try:
        log = LOGS / "litellm.out"
        start = log.stat().st_size
        tok = mint("alice@corp.example")["access_token"]
        litellm_chat(tok, [{"role": "user", "content": "a8b debug log"}])
        owui_chat(owui_session("alice@corp.example"), "a8b debug log through Open WebUI")
        time.sleep(1.5)
        with open(log, "rb") as f:
            f.seek(start)
            data = f.read()
        sig = tok.split(".")[2].encode()
        record("A8", "No token in LiteLLM's --detailed_debug log", not JWT_RE.findall(data) and sig not in data,
               f"JWT-shaped strings: {len(JWT_RE.findall(data))}; the minted token: {sig in data}")
    finally:
        svc("restart", "litellm")
    save("debug")


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "core"
    TESTER_SECRET = ensure_tester()
    steps = {"core": [core], "leaks": [leaks], "refresh": [refresh], "forward": [forward], "debug": [debug],
             "all": [core, refresh, forward, debug, leaks]}[what]
    for step in steps:
        step()
    print("\n" + "\n".join(f"{r['status']:4} {r['id']:5} {r['check']}" for r in results))
