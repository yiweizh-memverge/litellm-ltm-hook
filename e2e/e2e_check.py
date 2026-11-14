"""End to end with the real platform: Open WebUI and Claude Code, through LiteLLM, to the real
memmachine-gateway and MemMachine core, with real semantic search.

    $PY e2e/e2e_check.py          # run from the repo root; restarts the gateway and LiteLLM itself

Real: Keycloak 26.7.5 (realm corp), Open WebUI 0.12.5, LiteLLM 1.98.0, Claude Code with mm-auth
(t3/plugin), memmachine-gateway built from MemVerge/MemMachine-Platform main, MemMachine core
0.4.1-a1 (the chart's pinned image) on Postgres and Qdrant, and a real embedding model
(DeepInfra's BAAI/bge-m3 with e2e/core-cfg-deepinfra.yml, or all-MiniLM-L6-v2 locally with
e2e/core-cfg.yml). The chat model is the stand-in t1/fake_provider.py in phases M and X, which
answers "OK: <prompt>" and lists every memory line it was handed, and DeepInfra's Llama 3.3 70B in
phase R.

Phase M, the gateway exactly as on main (it accepts a token whose aud names "account"), the
LiteLLM hook forwarding each Open WebUI user's own token (#1561's fallback mode):
  M1  Open WebUI: alice's turns are stored in core, one episode per message
  M2  Open WebUI: a new chat, asked in other words, gets alice's fact ranked first of her memories
  M3  Open WebUI: bob, with memories of his own, gets none of alice's
  M4  Claude Code (carol, mm-auth straight to the gateway): stored in one session, recalled in the next
  M5  Claude Code through LiteLLM's pass-through route, LiteLLM checking its own key (Option A)
  M6  Claude Code through LiteLLM's pass-through route, no LiteLLM key (Option B)
  M7  One person across clients: alice in Claude Code recalls what she told Open WebUI
  M8  Isolation across clients: carol in Claude Code gets none of alice's memory
  M9  No corporate token reaches the model provider or LiteLLM's spend logs; Claude Code's model
      calls are billed to the developer's own LiteLLM key
  G*  INFO: which tokens main's gateway admits (no token, forged, ID token, exchanged, a client's own)

Phase X, the gateway with the e2e test patch (--audience memmachine-platform, #1561's audience),
the hook exchanging Open WebUI's token at Keycloak (#1561's recommended mode):
  X1  Open WebUI in exchange mode: stored and recalled through the real gateway
  X2  The platform got only exchanged tokens: the hook exchanged, and a corporate token sent
      straight to the gateway is refused
  X3  Claude Code (mm-auth's token names the platform) still stores and recalls
  X4  INFO: which tokens the patched gateway admits

Phase R, as phase M but with a real chat model (DeepInfra's Llama 3.3 70B) in place of the stand-in:
  R1  Open WebUI: told a fact in one chat, the model answers with it in a new chat
  R2  Open WebUI: told a fact in Korean, asked in English, the model answers with it (bge-m3 is multilingual)
  R3  Claude Code (carol): told a fact in one session, the model answers with it in the next

Afterwards the gateway is left as on main and LiteLLM back on t1/litellm-config.yaml.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "t1"), str(ROOT / "tools")]
import run_checks as rc  # noqa: E402

GW = "http://127.0.0.1:18110"
E = rc.T1_ENV / "real"
T3E = rc.T1_ENV / "t3"
PY = rc.T1_ENV / "litellm-venv" / "bin" / "python"
PLUGIN = ROOT / "t3" / "plugin"
MM = str(PLUGIN / "mm-auth")
CCONF, PROJ = T3E / "claude-config", T3E / "project"
TAG = uuid.uuid4().hex[:6]
PLATFORM = "memmachine-platform"
XENV = rc.env_file("exchange.env")
DEEPINFRA = E / "deepinfra.env"                   # DEEPINFRA_API_KEY=..., mode 600


# ------------------------------------------------------------------ services

def restart(gateway_audience=None, token_mode=None):
    """The gateway (main, or the patched build accepting gateway_audience) and LiteLLM (the e2e
    config and the hook in token_mode against the real gateway, or T1's own set-up when None)."""
    genv = {k: v for k, v in os.environ.items() if k != "E2E_AUDIENCE"}
    if gateway_audience:
        genv["E2E_AUDIENCE"] = gateway_audience
    subprocess.run(["e2e/svc.sh", "restart", "gateway"], cwd=ROOT, env=genv, check=True, capture_output=True)
    lenv = {k: v for k, v in os.environ.items() if not k.startswith("T1_") and k != "LITELLM_CONFIG"}
    if token_mode:
        lenv.update(LITELLM_CONFIG="../e2e/litellm-config.yaml", T1_API="platform", T1_GATEWAY_URL=GW,
                    T1_TOKEN_MODE=token_mode)
        if token_mode == "exchange":
            lenv.update(XENV)
        if DEEPINFRA.exists():
            lenv.update(rc.env_file("real/deepinfra.env"))
    subprocess.run(["t1/svc.sh", "restart", "litellm"], cwd=ROOT, env=lenv, check=True, capture_output=True)


# ------------------------------------------------------------------ tokens and the gateway

def person(email):
    return rc.mint(email)["access_token"]          # t1-tester's: aud litellm and account


def exchanged(token):
    _, js, _ = rc.http("POST", f"{rc.KC}/realms/{rc.REALM}/protocol/openid-connect/token", form={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token", "audience": PLATFORM,
        "client_id": XENV["T1_EXCHANGE_CLIENT_ID"], "client_secret": XENV["T1_EXCHANGE_CLIENT_SECRET"]})
    return js.get("access_token")


def gw_token(email, phase):
    t = person(email)
    return exchanged(t) if phase == "X" else t


def search(token, query, k=5):
    s, js, _ = rc.http("POST", f"{GW}/v2/memories/search", {"query": query, "top_k": k, "types": ["episodic"]},
                       {"Authorization": f"Bearer {token}"} if token else {})
    if s != 200:
        return s, (js.get("message") if isinstance(js, dict) else str(js))[:110]
    ltm = js["content"]["episodic_memory"]["long_term_memory"]["episodes"]
    return s, [f'{e.get("producer_role")}: {e["content"]}' for e in ltm]


def reset(email, phase="M"):
    t = gw_token(email, phase)
    rc.http("POST", f"{GW}/v2/projects/delete", {}, {"Authorization": f"Bearer {t}"})
    for _ in range(20):                         # core answers a search 500 while it finishes the delete
        if search(t, "anything")[0] == 200:
            return
        time.sleep(0.5)


def listed(token):
    """Every episode the person has, from the list API: unlike search, it neither ranks nor lags the store."""
    s, js, _ = rc.http("POST", f"{GW}/v2/memories/list", {"page_size": 500, "page_num": 0, "type": "episodic"},
                       {"Authorization": f"Bearer {token}"})
    if s != 200:
        return []
    return [f'{e.get("producer_role")}: {e["content"]}' for e in js["content"].get("episodic_memory") or []]


def wait_stored(email, phase, needle, query=None, n=1, timeout=30):
    """Until the person's memory holds n episodes containing needle (stores run after the reply), then
    a few seconds more: a stored episode reaches search shortly after the list."""
    t, end, eps = gw_token(email, phase), time.time() + timeout, []
    while time.time() < end:
        eps = listed(t)
        if sum(needle in e for e in eps) >= n:
            time.sleep(4)
            return eps
        time.sleep(1)
    return eps


def injected_lines(reply):
    """The memory lines the fake model lists in its answer, in the order it was handed them."""
    lines, on = [], False
    for line in (reply or "").splitlines():
        if line.startswith("[memory injected:"):
            on = True
        elif on and line.startswith("- "):
            lines.append(line[2:])
    return lines


# ------------------------------------------------------------------ the clients

def owui(email, text, model=rc.MODEL):
    session = rc.owui_session(email)
    # Open WebUI refreshes its model list only when asked for it, as its chat page does on load
    rc.http("GET", f"{rc.OWUI}/api/models", None, {"Authorization": f"Bearer {session[1]}"}, opener=session[0])
    _, reply = rc.owui_chat(session, text, model=model)
    return reply


def dev_key(email):
    """The person's own LiteLLM key, made once (user_id the person, models fake, no admin role)."""
    f = E / f"dev-key-{email.split('@')[0]}"
    master = rc.env_file("litellm.env")["LITELLM_MASTER_KEY"]
    if not f.exists():
        s, js, _ = rc.http("POST", f"{rc.LITELLM}/key/generate",
                           {"user_id": email.split("@")[0], "key_alias": f"e2e-dev-{email.split('@')[0]}",
                            "models": ["fake", "llama"]}, {"Authorization": f"Bearer {master}"})
        assert s == 200, js
        f.write_text(js["key"])
        f.chmod(0o600)
    key = f.read_text().strip()
    rc.http("POST", f"{rc.LITELLM}/key/update", {"key": key, "models": ["fake", "llama"]},
            {"Authorization": f"Bearer {master}"})
    return key


def mm_config(name, **kw):
    cfg = json.loads((PLUGIN / "config.json").read_text())
    cfg.update({"api": GW, "api_style": "platform", "top_k": 5, **kw})
    p = E / f"mm-auth-{name}.json"
    p.write_text(json.dumps(cfg))
    p.chmod(0o600)
    return p


class Dev:
    """A developer's machine: mm-auth's home and configuration, and their LiteLLM key."""

    def __init__(self, email, config, model="fake"):
        self.email, self.config, self.model = email, config, model
        self.home = E / f"mm-auth-home-{email.split('@')[0]}"
        self.key = dev_key(email)

    def mm(self, *args, env=None):
        return subprocess.run([MM, *args], capture_output=True, text=True, timeout=90, env={
            **os.environ, "MM_AUTH_HOME": str(self.home), "MM_AUTH_CONFIG": str(self.config), **(env or {})})

    def login(self):
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)
        return self.mm("login", env={"MM_AUTH_BROWSER": f"{PY} {ROOT / 't3' / 'fake_browser.py'} {self.email}"})

    def token(self):
        return self.mm("token", env={"MM_AUTH_PRINT_TOKEN": "1"}).stdout.strip()

    def claude(self, prompt):
        env = {"PATH": f"/usr/bin:/bin:{Path.home() / '.local/bin'}", "HOME": str(Path.home()), "TERM": "dumb",
               "CLAUDE_CONFIG_DIR": str(CCONF), "MM_AUTH_HOME": str(self.home), "MM_AUTH_CONFIG": str(self.config),
               "ANTHROPIC_BASE_URL": rc.LITELLM, "ANTHROPIC_API_KEY": self.key, "ANTHROPIC_MODEL": self.model,
               "ANTHROPIC_DEFAULT_HAIKU_MODEL": "fake", "ANTHROPIC_SMALL_FAST_MODEL": "fake",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1"}
        p = subprocess.run(["claude", "-p", prompt, "--plugin-dir", str(PLUGIN), "--output-format", "stream-json",
                            "--verbose", "--include-hook-events"], cwd=PROJ, env=env, capture_output=True, text=True,
                           timeout=180)
        result, msg = None, None
        for line in p.stdout.splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "result":
                result = e.get("result")
            if e.get("type") == "system" and e.get("subtype") == "hook_response" \
                    and e.get("hook_event") == "UserPromptSubmit":
                try:
                    msg = json.loads(e.get("output") or "{}").get("systemMessage")
                except json.JSONDecodeError:
                    pass
        return result, msg


def cc_round_trip(cid, name, dev, phase, fact, question, needle):
    """Claude Code: the fact in one session, the question in a new one; the fact must reach the model,
    ranked first (mm-auth's block puts the best-ranked episode last)."""
    t0 = time.time()
    r1, m1 = dev.claude(fact)
    stored = wait_stored(dev.email, phase, needle, question, n=2)
    r2, m2 = dev.claude(question)
    lines = injected_lines(r2)
    ok = bool(r1) and sum(needle in e for e in stored) >= 2 and bool(lines) and needle in lines[-1] and not m2
    rc.record(cid, name, ok, f"stored episodes with the fact: {sum(needle in e for e in stored)} (user + answer); "
                             f"next session's model got {len(lines)} memory line(s), best-ranked: "
                             f"{(lines[-1][:90] if lines else None)!r}; hook messages {m1!r}, {m2!r}; "
                             f"{time.time() - t0:.1f} s")
    return t0


# ------------------------------------------------------------------ phase M

def gateway_admits(cid, label):
    alice = rc.mint("alice@corp.example")
    cases = [("no token", None), ("forged signature", alice["access_token"][:-6] + "AAAAAA"),
             ("alice's ID token", alice["id_token"]), ("alice's corporate access token", alice["access_token"]),
             ("alice's token exchanged for the platform", exchanged(alice["access_token"])),
             ("a client's own token (client credentials, no person)",
              rc.mint(grant="client_credentials")["access_token"])]
    out = []
    for what, tok in cases:
        s, _ = search(tok, "probe")
        aud = rc.claims(tok).get("aud") if tok and tok.count(".") == 2 and what != "forged signature" else None
        out.append(f"{what}{f' (aud {aud})' if aud else ''}: {s}")
    rc.record(cid, f"Which tokens {label} admits", None, "; ".join(out))


def phase_m():
    restart(token_mode="forward")
    for who in ("alice@corp.example", "bob@corp.example", "carol@corp.example"):
        reset(who)
    t0 = time.time()

    distractors = ["Our team stand-up moves to 10am on Thursdays.", "I am reading a book about Roman history.",
                   "The quarterly report is due at the end of the month."]
    fact = f"My dog is a corgi called Biscuit-{TAG}."
    for d in distractors + [fact]:
        owui("alice@corp.example", d)
    stored = wait_stored("alice@corp.example", "M", f"Biscuit-{TAG}", "dog", n=2)
    total = len(listed(person("alice@corp.example")))
    rc.record("M1", "Open WebUI: alice's turns stored in core, one episode per message",
              sum(f"Biscuit-{TAG}" in e for e in stored) == 2 and total >= 8,
              f"alice's episodes in core: {total} (4 turns, prompt + answer each); with the fact: "
              f"{sum(f'Biscuit-{TAG}' in e for e in stored)}")

    reply = owui("alice@corp.example", "What kind of pet do I have, and what is its name?")
    lines = injected_lines(reply)
    rc.record("M2", "Open WebUI: a new chat, asked in other words, gets alice's fact ranked first",
              bool(lines) and f"Biscuit-{TAG}" in lines[0],
              f"{len(lines)} memory line(s) reached the model; first: {(lines[0][:90] if lines else None)!r}")

    owui("bob@corp.example", f"My cat is called Pepper-{TAG}.")
    wait_stored("bob@corp.example", "M", f"Pepper-{TAG}", "cat", n=2)
    lines = injected_lines(owui("bob@corp.example", "What kind of pet do I have, and what is its name?"))
    rc.record("M3", "Open WebUI: bob, with memories of his own, gets none of alice's",
              bool(lines) and f"Pepper-{TAG}" in lines[0] and not any(f"Biscuit-{TAG}" in l for l in lines),
              f"bob's model got {len(lines)} line(s), first {(lines[0][:70] if lines else None)!r}; "
              f"any of alice's: {any(f'Biscuit-{TAG}' in l for l in lines)}")

    carol = Dev("carol@corp.example", mm_config("direct"))
    carol.login()
    cc_round_trip("M4", "Claude Code (carol, mm-auth straight to the gateway): stored, recalled in a new session",
                  carol, "M", f"Remember this: I drive a blue Volvo, plate code {TAG}-D.", "Which car do I drive?",
                  f"{TAG}-D")

    t_route = time.time()
    carol_a = Dev("carol@corp.example", mm_config("via-litellm-a", api=f"{rc.LITELLM}/memmachine",
                                                  litellm_key=(T3E / "shared-route-key").read_text().strip()))
    carol_a.home = carol.home
    cc_round_trip("M5", "Claude Code through LiteLLM's pass-through route, LiteLLM checking its own key (Option A)",
                  carol_a, "M", f"Remember this: my favourite hiking trail is Eagle Ridge {TAG}-A.",
                  "Where do I like to go hiking?", f"{TAG}-A")
    carol_b = Dev("carol@corp.example", mm_config("via-litellm-b", api=f"{rc.LITELLM}/memmachine-open"))
    carol_b.home = carol.home
    cc_round_trip("M6", "Claude Code through LiteLLM's pass-through route, no LiteLLM key (Option B)",
                  carol_b, "M", f"Remember this: my sister lives in Lisbon, code {TAG}-B.",
                  "Which city does my sister live in?", f"{TAG}-B")
    passed = [e for e in rc.log_since("probe.jsonl", t_route) if e.get("call_type") == "pass_through_endpoint"]
    rc.record("M5/6", "The Option A and B memory calls went through LiteLLM's pass-through routes", bool(passed) or None,
              f"pass-through calls LiteLLM's callbacks saw: {len(passed)}")

    alice = Dev("alice@corp.example", mm_config("direct"))
    alice.login()
    # asked unlike her Open WebUI question, which is in her memory too and would outrank the fact
    r, m = alice.claude("What breed is my dog?")
    lines = injected_lines(r)
    hits = [i for i, l in enumerate(reversed(lines)) if l.startswith("user: ") and f"Biscuit-{TAG}" in l]
    rc.record("M7", "One person across clients: alice in Claude Code recalls what she told Open WebUI",
              bool(hits) and not m,
              f"the fact she told Open WebUI is in Claude Code's memory block: {bool(hits)}, rank "
              f"{hits[0] + 1 if hits else None} of {len(lines)} line(s); best-ranked {(lines[-1][:90] if lines else None)!r}")
    r, _ = carol.claude("What kind of pet do I have?")
    lines = injected_lines(r)
    rc.record("M8", "Isolation across clients: carol in Claude Code gets none of alice's or bob's memory",
              bool(lines) and not any(f"Biscuit-{TAG}" in l or f"Pepper-{TAG}" in l for l in lines),
              f"carol's model got {len(lines)} line(s); any of alice's or bob's: "
              f"{any(f'Biscuit-{TAG}' in l or f'Pepper-{TAG}' in l for l in lines)}")

    leaks(t0, "M9")
    gateway_admits("G", "main's gateway")


def leaks(t0, cid):
    prov = [e for e in rc.log_since("provider.jsonl", t0) if e.get("path", "").endswith("/chat/completions")]
    jwts = len(rc.JWT_RE.findall(json.dumps(prov).encode()))
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t0))
    out = subprocess.run(["docker", "exec", "t1-tokenpath-postgres-1", "psql", "-U", "litellm", "-p", "15432", "-h",
                          "127.0.0.1", "-d", "litellm", "-At", "-c",
                          f"SELECT row_to_json(s)::text FROM \"LiteLLM_SpendLogs\" s WHERE \"startTime\" >= '{since}'"],
                         capture_output=True, text=True).stdout
    spend_jwts = len(rc.JWT_RE.findall(out.encode()))
    rows = [json.loads(r) for r in out.splitlines() if r.strip()]
    cc = [r for r in rows if "messages" in (r.get("call_type") or "")]
    owners = sorted({r.get("user") for r in cc})
    rc.record(cid, "No corporate token reached the model provider or LiteLLM's spend logs; Claude Code's model "
                   "calls billed to each developer's own key",
              bool(prov) and jwts == 0 and spend_jwts == 0 and bool(cc) and set(owners) <= {"alice", "carol"},
              f"{len(prov)} provider requests, JWTs in them {jwts}; {len(rows)} spend-log rows, JWTs in them "
              f"{spend_jwts}; Claude Code's {len(cc)} model calls billed to {owners}")


# ------------------------------------------------------------------ phase R

def phase_r():
    """A real chat model: the memory has to change its answer. The gateway and hook stay as in phase M."""
    lm = "memmachine-llama"
    fact = f"My parking spot at the office is P3-{TAG}."
    owui("alice@corp.example", fact, model=lm)
    stored = wait_stored("alice@corp.example", "M", f"P3-{TAG}", "parking", n=1)
    reply = owui("alice@corp.example", "Where do I park at the office? Answer with the spot only.", model=lm)
    rc.record("R1", "Open WebUI, a real model: told a fact in one chat, it answers with it in a new chat",
              any(f"P3-{TAG}" in e for e in stored) and f"P3-{TAG}".lower() in (reply or "").lower(),
              f"stored: {any(f'P3-{TAG}' in e for e in stored)}; Llama answered {(reply or '')[:120]!r}")

    owui("alice@corp.example", f"제가 제일 좋아하는 음식은 김치찌개예요. 메모 코드는 {TAG}-K 입니다.", model=lm)
    stored = wait_stored("alice@corp.example", "M", f"{TAG}-K", "food", n=1)
    reply = owui("alice@corp.example", "What is my favourite food? Give its name and the memo code.", model=lm)
    rc.record("R2", "Open WebUI, a real model: told a fact in Korean, asked in English, it answers with it",
              any(f"{TAG}-K" in e for e in stored) and f"{TAG}-K".lower() in (reply or "").lower(),
              f"stored: {any(f'{TAG}-K' in e for e in stored)}; Llama answered {(reply or '')[:160]!r}")

    carol = Dev("carol@corp.example", mm_config("direct"), model="llama")
    r1, _ = carol.claude(f"Remember this for later: our staging server is called falcon-{TAG}. Just acknowledge.")
    stored = wait_stored("carol@corp.example", "M", f"falcon-{TAG}", "staging server", n=1)
    r2, m2 = carol.claude("What is the name of our staging server? Answer with the name only, use no tools.")
    rc.record("R3", "Claude Code, a real model: told a fact in one session, it answers with it in the next",
              any(f"falcon-{TAG}" in e for e in stored) and f"falcon-{TAG}" in (r2 or "") and not m2,
              f"stored: {any(f'falcon-{TAG}' in e for e in stored)}; first answer {(r1 or '')[:80]!r}; "
              f"Llama answered {(r2 or '')[:120]!r}; hook message {m2!r}")


# ------------------------------------------------------------------ phase X

def phase_x():
    restart(gateway_audience=PLATFORM, token_mode="exchange")
    reset("alice@corp.example", "X")
    t0 = time.time()
    fact = f"My favourite tea is a smoky lapsang souchong, code {TAG}-X."
    owui("alice@corp.example", "I am reading a book about Roman history.")
    owui("alice@corp.example", fact)
    stored = wait_stored("alice@corp.example", "X", f"{TAG}-X", "tea", n=2)
    lines = injected_lines(owui("alice@corp.example", "What do I like to drink?"))
    rc.record("X1", "Open WebUI in exchange mode: stored and recalled through the real gateway",
              sum(f"{TAG}-X" in e for e in stored) == 2 and bool(lines) and f"{TAG}-X" in lines[0],
              f"stored with the fact: {sum(f'{TAG}-X' in e for e in stored)}; the model got {len(lines)} line(s), "
              f"first {(lines[0][:80] if lines else None)!r}")

    hook = [e for e in rc.log_since("hook.jsonl", t0)]
    ex = [e for e in hook if e.get("event") == "exchange"]
    calls = [e for e in hook if e.get("event") in ("search", "store")]
    direct, _ = search(person("alice@corp.example"), "tea")
    rc.record("X2", "The platform got only exchanged tokens: the hook exchanged, a corporate token sent straight "
                    "to the gateway is refused",
              bool(ex) and all(e.get("status") == 200 or e.get("cached") for e in ex)
              and calls and all(e.get("status") == 200 for e in calls) and direct == 401,
              f"exchanges {len(ex)} ({sum(1 for e in ex if e.get('cached'))} from cache); memory calls "
              f"{[(e['event'], e.get('status')) for e in calls]}; alice's corporate token at the gateway: {direct}")

    carol = Dev("carol@corp.example", mm_config("direct"))
    aud = rc.claims(carol.token()).get("aud")
    cc_round_trip("X3", f"Claude Code (mm-auth's token, aud {aud}) stores and recalls through the patched gateway",
                  carol, "X", f"Remember this: my desk is on floor 7, code {TAG}-F.", "Which floor is my desk on?",
                  f"{TAG}-F")
    gateway_admits("X4", "the patched gateway (--audience memmachine-platform)")


def main():
    rc.TESTER_SECRET = rc.ensure_tester()
    print(f"run tag {TAG}")
    try:
        phase_m()
        if DEEPINFRA.exists():
            phase_r()
        phase_x()
    finally:
        restart()
    rc.save("e2e-real")
    print("\n" + "\n".join(f"{r['status']:4} {r['id']:5} {r['check']}" for r in rc.results))


if __name__ == "__main__":
    main()
