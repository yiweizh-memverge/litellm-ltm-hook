"""T4: the user's token exchanged by the hook, end to end (#1561's recommended mode).

    $PY t1/t4_exchange.py setup     # Keycloak: LiteLLM's exchange client, the platform's client,
                                    # LiteLLM's audience on Open WebUI's and t1-tester's tokens;
                                    # writes the client secret to $T1_ENV/exchange.env
    $PY t1/t4_exchange.py check     # with the gateway and LiteLLM in exchange mode (README)

Keycloak's side (standard exchange, its refusals, no service account) is in the T2 matrix; this
checks the hook's side, on SK's LiteLLM 1.98.0 through Open WebUI 0.12.5:

  X1  memory round trip through Open WebUI in exchange mode
  X2  the platform never receives the corporate token: every gateway request carries an exchanged
      token (azp LiteLLM, audience the platform alone)
  X3  the gateway refuses a corporate token sent to it directly
  X4  a user token is exchanged once, then served from the hook's cache
  X5  the cache ends at the user token's expiry, not the exchanged token's: a replayed expired
      user token gets no memory, and the gateway sees nothing for it
  X6  a token not issued for LiteLLM: exchange refused, no memory, the refusal remembered
  X7  LiteLLM's client has no service account: no token of its own
  X8  exchange latency (uncached, loopback)
"""
import json
import secrets
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_checks as rc  # noqa: E402

PLATFORM = "memmachine-platform"
SECRET_FILE = rc.T1_ENV / "exchange.env"


def aud_mapper(client):
    return {"name": f"aud-{client}", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
            "config": {"included.client.audience": client, "access.token.claim": "true", "id.token.claim": "false"}}


def setup():
    t = rc.kc_admin()
    h = {"Authorization": f"Bearer {t}"}
    base = f"{rc.KC}/admin/realms/{rc.REALM}/clients"
    if rc.kc_client(t, PLATFORM) is None:
        rc.http("POST", base, {"clientId": PLATFORM, "enabled": True, "publicClient": False,
                               "standardFlowEnabled": False, "directAccessGrantsEnabled": False,
                               "serviceAccountsEnabled": False}, h)
    secret = secrets.token_urlsafe(24)
    lite = {"clientId": "litellm", "enabled": True, "publicClient": False, "secret": secret,
            "standardFlowEnabled": False, "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False,
            "attributes": {"standard.token.exchange.enabled": "true", "access.token.lifespan": "300"},
            "protocolMappers": [aud_mapper(PLATFORM)]}
    c = rc.kc_client(t, "litellm")
    if c is None:
        assert rc.http("POST", base, lite, h)[0] == 201
    else:
        c.update({k: v for k, v in lite.items() if k != "protocolMappers"})
        assert rc.http("PUT", f"{base}/{c['id']}", c, h)[0] == 204
    for name in ("open-webui", "t1-tester"):
        c = rc.kc_client(t, name)
        _, mappers, _ = rc.http("GET", f"{base}/{c['id']}/protocol-mappers/models", headers=h)
        if not any(m.get("name") == "aud-litellm" for m in mappers or []):
            assert rc.http("POST", f"{base}/{c['id']}/protocol-mappers/models", aud_mapper("litellm"), h)[0] == 201
    SECRET_FILE.write_text(f"T1_EXCHANGE_CLIENT_ID=litellm\nT1_EXCHANGE_CLIENT_SECRET={secret}\n")
    SECRET_FILE.chmod(0o600)
    print(f"exchange client ready; secret in {SECRET_FILE}")


def gw_since(t0):
    return [e for e in rc.log_since("gateway.jsonl", t0) if e.get("path", "").startswith("/api/v2/memories")]


def hook_since(t0, event=None):
    return [e for e in rc.log_since("hook.jsonl", t0) if event is None or e.get("event") == event]


def chat(token, text):
    mid = str(uuid.uuid4())
    s, js, _ = rc.litellm_chat(token, [{"role": "user", "content": text}],
                               {"x-openwebui-message-id": mid, "x-openwebui-task": "", "x-openwebui-model-id": rc.MODEL})
    time.sleep(1)
    reply = js["choices"][0]["message"]["content"] if s == 200 else ""
    return s, reply, mid


def check():
    rc.TESTER_SECRET = rc.ensure_tester()
    secret = dict(l.split("=", 1) for l in SECRET_FILE.read_text().split())["T1_EXCHANGE_CLIENT_SECRET"]

    # X1, X2: through Open WebUI
    alice = rc.owui_session("alice@corp.example")
    tag = uuid.uuid4().hex[:6]
    t0 = time.time()
    rc.owui_chat(alice, f"t4 fact {tag}: my codename is heron-{tag}")
    time.sleep(1.5)
    _, reply = rc.owui_chat(alice, "what is my codename?")
    time.sleep(1)
    rc.record("X1", "Memory round trip through Open WebUI, exchange mode", f"heron-{tag}" in reply,
              f"memory items in the reply: {rc.injected(reply)}")
    seen = gw_since(t0)
    shapes = sorted({(str((e.get("claims") or {}).get("azp")), json.dumps((e.get("claims") or {}).get("aud")))
                     for e in seen})
    rc.record("X2", "Every gateway request carried an exchanged token, never the corporate one",
              bool(seen) and all((e.get("claims") or {}).get("azp") == "litellm" for e in seen),
              f"{len(seen)} gateway requests; (azp, aud) seen: {shapes}")
    # gateway claims log keeps iss/sub/azp/typ/exp; aud is checked by the gateway itself (X3)

    # X3: the corporate token straight to the gateway
    corp = rc.mint("alice@corp.example")["access_token"]
    s, reason = rc.gateway_search(corp)
    rc.record("X3", "The gateway refuses a corporate token sent to it directly", s == 401,
              f"HTTP {s}, {reason}; token azp {rc.claims(corp).get('azp')}, aud {rc.claims(corp).get('aud')}")

    # X4, X5: cache, and its end at the user token's expiry
    user = rc.mint("alice@corp.example")["access_token"]
    t1 = time.time()
    chat(user, f"t4 cache {tag} one")
    chat(user, f"t4 cache {tag} two")
    ex = [e for e in hook_since(t1, "exchange") if e.get("token") == rc.hashlib.sha256(user.encode()).hexdigest()[:12]]
    first = next((e for e in ex if not e.get("cached")), {})
    rc.record("X4", "A user token is exchanged once, then served from the cache",
              [e.get("cached") for e in ex][:1] == [False] and any(e.get("cached") for e in ex[1:]),
              f"exchange events {[('cached' if e.get('cached') else e.get('status')) for e in ex]}")
    rc.record("X5", "The cached exchanged token is usable no longer than the user token",
              bool(first) and first.get("usable_s", 999) <= first.get("user_s", 0) < first.get("exchanged_s", 0),
              f"usable {first.get('usable_s')} s, user token {first.get('user_s')} s, exchanged token {first.get('exchanged_s')} s")
    wait = rc.claims(user)["exp"] - time.time() + 2
    time.sleep(max(wait, 0))
    t2 = time.time()
    s, reply, _ = chat(user, f"t4 cache {tag} after expiry")
    ex2 = [e for e in hook_since(t2, "exchange")]
    rc.record("X5", "A replayed expired user token: no memory, nothing reaches the gateway",
              s == 200 and rc.injected(reply) == 0 and not gw_since(t2)
              and any(not e.get("cached") and e.get("outcome") == "refused" for e in ex2),
              f"chat HTTP {s}; memory {rc.injected(reply)}; gateway requests {len(gw_since(t2))}; "
              f"exchange {[(e.get('status'), e.get('error')) for e in ex2]}")

    # X6: a token not issued for LiteLLM (no LiteLLM audience), twice
    s, js, _ = rc.http("POST", f"{rc.KC}/realms/{rc.REALM}/protocol/openid-connect/token", form={
        "grant_type": "password", "client_id": "admin-cli", "username": "alice@corp.example",
        "password": rc.accounts()["alice@corp.example"].strip(), "scope": "openid"})
    if s != 200:
        rc.record("X6", "A token not issued for LiteLLM", None, f"could not get one: HTTP {s} {js}")
    else:
        other = js["access_token"]
        t3 = time.time()
        r1 = chat(other, f"t4 other {tag} one")
        r2 = chat(other, f"t4 other {tag} two")
        ex3 = hook_since(t3, "exchange")
        rc.record("X6", "A token not issued for LiteLLM: exchange refused once, remembered, no memory",
                  r1[0] == r2[0] == 200 and rc.injected(r1[1]) == rc.injected(r2[1]) == 0 and not gw_since(t3)
                  and [e.get("cached", False) for e in ex3] == [False, True],
                  f"aud {rc.claims(other).get('aud')}; exchange events "
                  f"{[('cached ' + str(e.get('outcome')) if e.get('cached') else (e.get('status'), e.get('error'))) for e in ex3]}")

    # X7: LiteLLM's own token
    s, js, _ = rc.http("POST", f"{rc.KC}/realms/{rc.REALM}/protocol/openid-connect/token",
                       form={"grant_type": "client_credentials"}, headers={
                           "Authorization": "Basic " + rc.base64.b64encode(f"litellm:{secret}".encode()).decode()})
    rc.record("X7", "LiteLLM's client gets no token of its own", s in (400, 401),
              f"HTTP {s} {(js or {}).get('error')}")

    # X8: latency of uncached exchanges in this run
    ms = [e["ms"] for e in hook_since(t0, "exchange") if not e.get("cached") and e.get("status") == 200]
    rc.record("X8", "Exchange latency, uncached, loopback", None,
              f"{len(ms)} exchanges, median {statistics.median(ms) if ms else None} ms, max {max(ms) if ms else None} ms")
    rc.save("t4")
    print("\n" + "\n".join(f"{r['status']:4} {r['id']:3} {r['check']}" for r in rc.results))


if __name__ == "__main__":
    {"setup": setup, "check": check}[sys.argv[1]]()
