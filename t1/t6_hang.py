"""T6 gate check: what an Open WebUI chat does when Keycloak hangs or is down.

Open WebUI 0.12.x refreshes a person's Keycloak token before a chat when 5 minutes or
less are left (`OAuthManager.get_oauth_token`), with aiohttp's default 300 s timeout and
no timeout of its own (`_perform_token_refresh`). It does so from the chat middleware
(`process_chat_payload` builds `__oauth_token__`) whatever the connection's auth type,
and again for a `system_oauth` connection's header. A failed refresh deletes the session.

    $PY t1/t6_hang.py           # ~15 minutes; pauses, stops and restarts Keycloak

Cases, run at the same time while Keycloak is paused (accepts connections, never answers):
  bob-1    system_oauth connection, one Open WebUI session, 300 s token
  bob-2    the same session, 5 s later (waits on Open WebUI's per-session refresh lock)
  carol    a connection with no user token (auth_type none): Open WebUI without approach A
  alice    system_oauth, two sessions (Open WebUI falls back to the most recent other one)
  admin    system_oauth, 900 s token, so no refresh is due
Then with Keycloak back: does each person still get their token, i.e. memory?
Then Keycloak stopped (connection refused): bob again, and after Keycloak restarts.
"""
import json
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_checks as rc  # noqa: E402

KC_CONTAINER = "t1-tokenpath-keycloak-1"
DB = rc.T1_ENV / "owui-data-v0.12.5" / "webui.db"
PLAIN = "plain"                       # prefix of the connection without a user token
OWUI_LOG = rc.LOGS / "openwebui.out"
out = []


def note(case, **kw):
    kw["case"] = case
    out.append(kw)
    print(json.dumps(kw), flush=True)


def docker(*args):
    subprocess.run(["docker", *args, KC_CONTAINER], check=True, capture_output=True)


def kc_ready(timeout=180):
    t = time.time()
    while time.time() - t < timeout:
        try:
            s, _, _ = rc.http("GET", f"{rc.KC}/realms/{rc.REALM}/.well-known/openid-configuration", timeout=3)
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def sessions(email):
    with sqlite3.connect(DB) as c:
        return c.execute("select count(*) from oauth_session s join user u on u.id = s.user_id "
                         "where u.email = ?", (email,)).fetchone()[0]


def clear_sessions(email):
    with sqlite3.connect(DB) as c:
        c.execute("delete from oauth_session where user_id in (select id from user where email = ?)", (email,))


def owui_log_counts(start):
    with open(OWUI_LOG, "rb") as f:
        f.seek(start)
        text = f.read()
    return {k: text.count(v) for k, v in {
        "refresh_ok": b"Successfully refreshed token",
        "refresh_exception": b"Exception during token refresh",
        "session_deleted": b"deleting session"}.items()}


def chat(session, text, model):
    """One prompt through Open WebUI; elapsed time, status, whether the hook got a user token."""
    opener, tok = session
    mid = str(uuid.uuid4())
    body = {"model": model, "stream": False, "chat_id": f"local:{uuid.uuid4()}", "id": mid,
            "messages": [{"role": "user", "content": text}],
            "user_message": {"id": str(uuid.uuid4()), "role": "user", "content": text}}
    t0 = time.time()
    try:
        s, js, _ = rc.http("POST", f"{rc.OWUI}/api/chat/completions", body,
                           {"Authorization": f"Bearer {tok}"}, opener=opener, timeout=1800)
        reply = js["choices"][0]["message"]["content"] if s == 200 else str(js)[:200]
    except Exception as e:
        s, reply = "error", repr(e)[:200]
    elapsed = round(time.time() - t0, 1)
    time.sleep(1)
    searched = [e for e in rc.log_since("hook.jsonl", t0) if e.get("turn") == mid and e["event"] == "search"]
    return {"elapsed_s": elapsed, "status": s, "answered": s == 200,
            "user_token_reached_hook": bool(searched),
            "gateway": searched[0].get("status") if searched else None,
            "reply": (reply or "")[:80]}


def connections(admin_tok, add_plain):
    h = {"Authorization": f"Bearer {admin_tok}"}
    _, cfg, _ = rc.http("GET", f"{rc.OWUI}/openai/config", headers=h)
    urls, keys, cfgs = cfg["OPENAI_API_BASE_URLS"][:1], cfg["OPENAI_API_KEYS"][:1], {"0": cfg["OPENAI_API_CONFIGS"]["0"]}
    if add_plain:
        headers = dict(cfgs["0"]["headers"])
        urls.append(urls[0])
        keys.append("")
        cfgs["1"] = {"auth_type": "none", "prefix_id": PLAIN, "headers": headers}
    s, _, _ = rc.http("POST", f"{rc.OWUI}/openai/config/update", {
        "ENABLE_OPENAI_API": True, "OPENAI_API_BASE_URLS": urls, "OPENAI_API_KEYS": keys,
        "OPENAI_API_CONFIGS": cfgs}, h)
    assert s == 200, s
    rc.http("GET", f"{rc.OWUI}/api/models?refresh=true", headers=h, timeout=120)


def main():
    users = ["alice@corp.example", "bob@corp.example", "carol@corp.example", "admin@corp.example"]
    for u in users:
        clear_sessions(u)
    rc.set_owui_lifespan(None)                                    # 300 s, the realm default
    bob, carol = rc.owui_session(users[1]), rc.owui_session(users[2])
    rc.owui_session(users[0])
    alice = rc.owui_session(users[0])                             # alice: two sessions
    rc.set_owui_lifespan(900)
    admin = rc.owui_session(users[3])                             # 900 s token
    rc.set_owui_lifespan(None)
    connections(alice[1], add_plain=True)
    plain_model = f"{PLAIN}.{rc.MODEL}"
    try:
        for name, sess, model in (("bob", bob, rc.MODEL), ("carol", carol, plain_model),
                                  ("alice", alice, rc.MODEL), ("admin", admin, rc.MODEL)):
            note("baseline, Keycloak up", who=name, model=model, **chat(sess, "t6 baseline", model))
        note("sessions before hang", **{u.split("@")[0]: sessions(u) for u in users})

        # ---- Keycloak hangs
        log_start = OWUI_LOG.stat().st_size
        docker("pause")
        hang_t0 = time.time()
        res = {}

        def run(name, sess, model, delay=0):
            time.sleep(delay)
            res[name] = chat(sess, f"t6 hang {name}", model)
            res[name]["started_s"] = delay

        threads = [threading.Thread(target=run, args=a) for a in (
            ("bob-1", bob, rc.MODEL), ("bob-2", bob, rc.MODEL, 5), ("carol", carol, plain_model),
            ("alice", alice, rc.MODEL), ("admin", admin, rc.MODEL))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        docker("unpause")
        note("Keycloak paused", paused_for_s=round(time.time() - hang_t0, 1))
        for name in ("bob-1", "bob-2", "carol", "alice", "admin"):
            note("hang", who=name, **res[name])
        note("Open WebUI log during hang", **owui_log_counts(log_start))
        note("sessions after hang", **{u.split("@")[0]: sessions(u) for u in users})

        kc_ready()
        for name, sess, model in (("bob", bob, rc.MODEL), ("carol", carol, rc.MODEL),
                                  ("alice", alice, rc.MODEL), ("admin", admin, rc.MODEL)):
            note("after hang, Keycloak up again", who=name, **chat(sess, "t6 after hang", model))

        # ---- Keycloak down: connection refused
        bob = rc.owui_session(users[1])
        note("baseline before stop", who="bob", **chat(bob, "t6 before stop", rc.MODEL))
        log_start = OWUI_LOG.stat().st_size
        docker("stop")
        try:
            note("Keycloak stopped", who="bob", **chat(bob, "t6 Keycloak stopped", rc.MODEL))
            note("Open WebUI log while stopped", **owui_log_counts(log_start))
        finally:
            docker("start")
            note("Keycloak restarted", ready=kc_ready())
        note("after restart", who="bob", sessions=sessions(users[1]), **chat(bob, "t6 after restart", rc.MODEL))
    finally:
        subprocess.run(["docker", "unpause", KC_CONTAINER], capture_output=True)
        subprocess.run(["docker", "start", KC_CONTAINER], capture_output=True)
        kc_ready()
        connections(rc.owui_session(users[0])[1], add_plain=False)
        path = rc.LOGS / "results-t6.json"
        path.write_text(json.dumps(out, indent=1))
        print(f"\nsaved {path}")


if __name__ == "__main__":
    main()
