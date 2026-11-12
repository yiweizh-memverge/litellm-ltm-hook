"""T2 pre-work: which Keycloak versions provide what approach A (#1561) asks of the provider.

For each version: a throwaway container, a clean realm like #1561's, and four groups of checks,
each issuing, using and refreshing real tokens, with a refusal case:

  contract   user access token RS256, typ Bearer, azp, platform audience; ID token typ ID;
             client-credentials token marked (client_id, or older clientId); same sub for one
             person across Open WebUI's, mm-auth's and the console's clients; per-client lifespan
  dpop       mm-auth: public client, PKCE code flow, a client policy (dpop-bind-enforcer,
             allow-only-refresh-token-binding, auto-configure off) binding only the refresh
             token: the code exchange needs a proof, the access token stays a bearer (no cnf),
             a refresh needs the same key's proof, two refreshes at once both succeed
  offline    offline_access for Open WebUI and mm-auth; the client's offline idle limit applies;
             a disabled person's refresh fails; whether re-enabling revives the old token
  exchange   standard token exchange by LiteLLM's client to the platform-only audience, same
             sub; refused: another audience, a subject token not naming LiteLLM, no secret
  device     (conditional) device authorization request on mm-auth's client, PKCE included

    $PY t2/kc_matrix.py 26.8.1 [--features dpop,token-exchange-standard] [--keep-image]

Results append to $T1_ENV/logs/kc-matrix.jsonl: token claims by name and selected values only,
never a token. Passwords and secrets are throwaway and local.
"""
import argparse
import base64
import hashlib
import html
import json
import re
import secrets
import threading
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from sso_browser import _LocalhostJar  # noqa: E402

T1_ENV = Path(os.environ.get("T1_ENV", Path.home() / "t1-tokenpath-env"))
OUT = T1_ENV / "logs" / "kc-matrix.jsonl"
PORT = 18190
BASE = f"http://127.0.0.1:{PORT}"
REALM = "corp"
TOKEN_URL = f"{BASE}/realms/{REALM}/protocol/openid-connect/token"
ADMIN_PW = "matrix-admin-pw"
ALICE_PW = "Alice-matrix-1"
REDIRECT = "http://127.0.0.1:1/callback"
OFFLINE_PROBE_IDLE = 15          # s; Keycloak adds a grace window of about 2 minutes to idle limits
OFFLINE_PROBE_WAIT = 150
SECRETS = {"open-webui": "owui-secret", "litellm": "litellm-secret", "memmachine-console": "console-secret", "offline-probe": "probe-secret",
           "memmachine-platform": "platform-secret", "other-api": "other-secret"}


# ------------------------------------------------------------------ HTTP

def http(method, url, form=None, body=None, headers=None, auth=None, timeout=30):
    h = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")[:200]


def claims(token):
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except Exception as e:
        return {"undecodable": repr(e)[:80]}


def header(token):
    return jwt.get_unverified_header(token)


def err(js):
    """Short form of an OAuth error answer."""
    if isinstance(js, dict):
        return {k: js[k] for k in ("error", "error_description") if k in js}
    return str(js)[:120]


# ------------------------------------------------------------------ container

def docker(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def start(version, features):
    docker("rm", "-f", "kc-matrix", check=False)
    cmd = ["run", "-d", "--name", "kc-matrix", "-p", f"127.0.0.1:{PORT}:8080",
           "-e", "KEYCLOAK_ADMIN=admin", "-e", f"KEYCLOAK_ADMIN_PASSWORD={ADMIN_PW}",
           "-e", "KC_BOOTSTRAP_ADMIN_USERNAME=admin", "-e", f"KC_BOOTSTRAP_ADMIN_PASSWORD={ADMIN_PW}",
           f"quay.io/keycloak/keycloak:{version}", "start-dev"]
    if features:
        cmd.append(f"--features={features}")
    docker(*cmd)
    t0 = time.time()
    while time.time() - t0 < 240:
        if docker("inspect", "-f", "{{.State.Running}}", "kc-matrix", check=False).stdout.strip() != "true":
            return False, docker("logs", "--tail", "15", "kc-matrix", check=False).stderr[-1500:]
        try:
            if http("GET", f"{BASE}/realms/master", timeout=3)[0] == 200:
                return True, round(time.time() - t0)
        except Exception:
            pass
        time.sleep(2)
    return False, "not ready in 240 s"


def stop():
    docker("rm", "-f", "kc-matrix", check=False)


# ------------------------------------------------------------------ realm

def admin_token():
    _, js = http("POST", f"{BASE}/realms/master/protocol/openid-connect/token",
                 form={"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": ADMIN_PW})
    return js["access_token"]


def aud_mapper(client):
    return {"name": f"aud-{client}", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
            "config": {"included.client.audience": client, "access.token.claim": "true", "id.token.claim": "false"}}


def realm_rep():
    def confidential(cid, **kw):
        return {"clientId": cid, "enabled": True, "publicClient": False, "secret": SECRETS[cid],
                "standardFlowEnabled": False, "directAccessGrantsEnabled": False, **kw}
    return {
        "realm": REALM, "enabled": True, "accessTokenLifespan": 300,
        "users": [{"username": "alice", "email": "alice@corp.example", "firstName": "Alice", "lastName": "Test",
                   "emailVerified": True, "enabled": True, "realmRoles": [f"default-roles-{REALM}"],
                   "credentials": [{"type": "password", "value": ALICE_PW, "temporary": False}]}],
        "roles": {"client": {"mm-auth": [{"name": "dpop-bound"}]}},
        "clients": [
            confidential("open-webui", standardFlowEnabled=True, directAccessGrantsEnabled=True,
                         redirectUris=["http://127.0.0.1/*"],
                         attributes={"access.token.lifespan": "900", "client.offline.session.idle.timeout": "86400"},
                         protocolMappers=[aud_mapper("litellm")]),
            confidential("litellm", serviceAccountsEnabled=True,
                         attributes={"standard.token.exchange.enabled": "true"},
                         protocolMappers=[aud_mapper("memmachine-platform")]),
            confidential("memmachine-platform"),
            confidential("memmachine-console", directAccessGrantsEnabled=True,
                         protocolMappers=[aud_mapper("memmachine-platform")]),
            confidential("other-api"),
            confidential("offline-probe", directAccessGrantsEnabled=True,
                         attributes={"client.offline.session.idle.timeout": str(OFFLINE_PROBE_IDLE)}),
            {"clientId": "mm-auth", "enabled": True, "publicClient": True, "standardFlowEnabled": True,
             "directAccessGrantsEnabled": False, "redirectUris": [REDIRECT],
             "attributes": {"oauth2.device.authorization.grant.enabled": "true", "pkce.code.challenge.method": "S256"},
             "protocolMappers": [aud_mapper("memmachine-platform")]},
        ],
    }


def setup(t):
    h = {"Authorization": f"Bearer {t}"}
    s, js = http("POST", f"{BASE}/admin/realms", body=realm_rep(), headers=h)
    assert s == 201, (s, js)
    prof = {"profiles": [{"name": "mm-auth-dpop", "description": "bind mm-auth's refresh token",
                          "executors": [{"executor": "dpop-bind-enforcer", "configuration": {
                              "auto-configure": "false", "allow-only-refresh-token-binding": "true"}}]}]}
    pol = {"policies": [{"name": "mm-auth-dpop", "enabled": True,
                         "conditions": [{"condition": "client-roles", "configuration": {"roles": ["dpop-bound"]}}],
                         "profiles": ["mm-auth-dpop"]}]}
    s1, j1 = http("PUT", f"{BASE}/admin/realms/{REALM}/client-policies/profiles", body=prof, headers=h)
    s2, j2 = http("PUT", f"{BASE}/admin/realms/{REALM}/client-policies/policies", body=pol, headers=h)
    _, saved = http("GET", f"{BASE}/admin/realms/{REALM}/client-policies/profiles", headers=h)
    saved_exec = [e for p in (saved or {}).get("profiles", []) for e in p.get("executors", [])]
    return {"profile": s1 if s1 < 300 else (s1, err(j1)), "policy": s2 if s2 < 300 else (s2, err(j2)),
            "saved_executors": saved_exec}


def server_info(t):
    _, si = http("GET", f"{BASE}/admin/serverinfo", headers={"Authorization": f"Bearer {t}"})
    feats = {}
    if isinstance(si.get("features"), list):                       # 26.x
        for f in si["features"]:
            if any(k in f["name"] for k in ("TOKEN_EXCHANGE", "DPOP", "DEVICE")):
                feats[f["name"]] = {"type": f.get("type"), "enabled": f.get("enabled")}
    pi = si.get("profileInfo") or {}
    for kind in ("disabledFeatures", "previewFeatures", "experimentalFeatures"):
        for name in pi.get(kind, []):
            if any(k in name for k in ("TOKEN_EXCHANGE", "DPOP", "DEVICE")):
                feats.setdefault(name, {})[kind] = True
    execs = si.get("componentTypes", {}).get(
        "org.keycloak.services.clientpolicy.executor.ClientPolicyExecutorProvider", [])
    dpop = next((e for e in execs if e["id"] == "dpop-bind-enforcer"), None)
    return {"version": si.get("systemInfo", {}).get("version"), "features": feats,
            "dpop_executor_options": [p["name"] for p in dpop["properties"]] if dpop else None}


# ------------------------------------------------------------------ sign-in helpers

KEY1 = ec.generate_private_key(ec.SECP256R1())      # mm-auth's key, one per installation
KEY2 = ec.generate_private_key(ec.SECP256R1())      # someone else's


def dpop_proof(key, htm="POST", htu=TOKEN_URL):
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    return jwt.encode({"jti": str(uuid.uuid4()), "htm": htm, "htu": htu, "iat": int(time.time())},
                      key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": jwk})


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class _StopAtRedirect(urllib.request.HTTPRedirectHandler):
    """Follow Keycloak's redirects, but stop at the client's redirect URI and keep its URL."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl.startswith(REDIRECT):
            raise _Arrived(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Arrived(Exception):
    def __init__(self, url):
        self.url = url


def browser_code(client_id, scope):
    """The browser half of a code flow: sign alice in on Keycloak's page, return (code, verifier)."""
    verifier, challenge = pkce()
    q = urllib.parse.urlencode({"client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT,
                                "scope": scope, "state": secrets.token_hex(8), "code_challenge": challenge,
                                "code_challenge_method": "S256"})
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_LocalhostJar()), _StopAtRedirect())
    try:
        page = opener.open(f"{BASE}/realms/{REALM}/protocol/openid-connect/auth?{q}", timeout=30).read().decode()
        action = html.unescape(re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', page).group(1))
        opener.open(urllib.request.Request(action, data=urllib.parse.urlencode(
            {"username": "alice", "password": ALICE_PW, "credentialId": ""}).encode()), timeout=30)
    except _Arrived as a:
        return urllib.parse.parse_qs(urllib.parse.urlparse(a.url).query)["code"][0], verifier
    raise RuntimeError("no code came back")


def code_flow(client_id, scope="openid", key=None, secret=None):
    """PKCE code flow; the code exchange carries a DPoP proof if a key is given."""
    code, verifier = browser_code(client_id, scope)
    form = {"grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": REDIRECT, "code_verifier": verifier}
    return http("POST", TOKEN_URL, form=form, auth=(client_id, secret) if secret else None,
                headers={"DPoP": dpop_proof(key)} if key else None)


def password_grant(client, scope="openid"):
    return http("POST", TOKEN_URL, form={"grant_type": "password", "client_id": client, "username": "alice",
                                         "password": ALICE_PW, "scope": scope}, auth=(client, SECRETS[client]))


def refresh(client, rt, key=None, secret=None):
    return http("POST", TOKEN_URL, form={"grant_type": "refresh_token", "client_id": client, "refresh_token": rt},
                auth=(client, secret) if secret else None, headers={"DPoP": dpop_proof(key)} if key else None)


def status(s, js, **ok):
    return {"status": s, **(ok if s == 200 else {"error": err(js)})}


def bearer_shape(js):
    """What a resource server sees: the token type Keycloak names, and whether the token is key-bound."""
    c = claims(js["access_token"])
    return {"token_type": js.get("token_type"), "access_cnf": "cnf" in c}


# ------------------------------------------------------------------ groups

def g_contract():
    out = {}
    s, js = password_grant("open-webui")
    if s != 200:
        return {"open_webui_sign_in": status(s, js)}
    at, idt = js["access_token"], js.get("id_token")
    c = claims(at)
    out["user_token"] = {"alg": header(at).get("alg"), "typ": c.get("typ"), "azp": c.get("azp"),
                         "aud": c.get("aud"), "client_id_claim": "client_id" in c,
                         "id_token_typ": claims(idt).get("typ") if idt else None,
                         "lifespan_s": c["exp"] - c["iat"], "realm_default_s": 300}
    s, cj = http("POST", TOKEN_URL, form={"grant_type": "client_credentials"}, auth=("litellm", SECRETS["litellm"]))
    cc = claims(cj["access_token"]) if s == 200 else {}
    out["client_credentials_token"] = status(s, cj, client_id=("client_id" in cc), clientId=("clientId" in cc),
                                             typ=cc.get("typ"))
    subs = {"open-webui": c.get("sub")}
    s, js = password_grant("memmachine-console")
    subs["memmachine-console"] = claims(js["access_token"]).get("sub") if s == 200 else f"error {s}"
    s, js = code_flow("mm-auth", key=KEY1)
    subs["mm-auth"] = claims(js["access_token"]).get("sub") if s == 200 else f"error {s}"
    if s == 200:
        out["platform_audience_mm_auth"] = claims(js["access_token"]).get("aud")
    out["same_sub_across_clients"] = len(set(subs.values())) == 1
    out["_sub"] = subs["open-webui"]
    return out


def g_dpop():
    out = {}
    s, js = code_flow("mm-auth")
    out["code_exchange_without_proof_refused"] = status(s, js)
    s, js = code_flow("mm-auth", scope="openid offline_access", key=KEY1)
    if s != 200:
        out["code_exchange_with_proof"] = status(s, js)
        return out
    rt = js["refresh_token"]
    out["code_exchange_with_proof"] = {"status": 200, **bearer_shape(js), "refresh_cnf": "cnf" in claims(rt),
                                       "refresh_typ": claims(rt).get("typ")}
    out["refresh_without_proof_refused"] = status(*refresh("mm-auth", rt))
    out["refresh_other_key_refused"] = status(*refresh("mm-auth", rt, KEY2))
    s, js = refresh("mm-auth", rt, KEY1)
    out["refresh_right_key"] = status(s, js, **(bearer_shape(js) if s == 200 else {}))
    if s == 200:
        rt = js.get("refresh_token", rt)
    res = [None, None]

    def go(i):
        res[i] = refresh("mm-auth", rt, KEY1)[0]
    ts = [threading.Thread(target=go, args=(i,)) for i in (0, 1)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    out["two_refreshes_at_once"] = res
    return out


def admin_user(t):
    h = {"Authorization": f"Bearer {t}"}
    _, users = http("GET", f"{BASE}/admin/realms/{REALM}/users?username=alice&exact=true", headers=h)
    return users[0], h


def set_enabled(t, on):
    u, h = admin_user(admin_token())         # the master realm's admin token lives 60 s
    u["enabled"] = on
    return http("PUT", f"{BASE}/admin/realms/{REALM}/users/{u['id']}", body=u, headers=h)[0]


def g_offline(t):
    out = {}
    s, js = password_grant("open-webui", scope="openid offline_access")
    if s != 200:
        return {"open_webui_offline": status(s, js)}
    rt = js["refresh_token"]
    rc = claims(rt)
    out["open_webui_offline"] = {"refresh_typ": rc.get("typ"),
                                 "refresh_lifetime_s": (rc["exp"] - rc["iat"]) if "exp" in rc else None,
                                 "client_offline_idle_set_s": 86400, "realm_offline_idle_s": 2592000}
    s, js = code_flow("mm-auth", scope="openid offline_access", key=KEY1)
    out["mm_auth_offline"] = status(s, js, refresh_typ=claims(js["refresh_token"]).get("typ") if s == 200 else None)
    # the client's own offline idle limit: two offline tokens, one used early, one only after the limit
    _, a = password_grant("offline-probe", scope="openid offline_access")
    _, b = password_grant("offline-probe", scope="openid offline_access")
    early = refresh("offline-probe", a["refresh_token"], secret=SECRETS["offline-probe"])[0]
    time.sleep(OFFLINE_PROBE_WAIT)
    s, js = refresh("offline-probe", b["refresh_token"], secret=SECRETS["offline-probe"])
    out["client_offline_idle_limit"] = {"limit_s": OFFLINE_PROBE_IDLE, "waited_s": OFFLINE_PROBE_WAIT,
                                        "refresh_within_limit": early, "refresh_after_limit": status(s, js)}
    out["refresh_while_enabled"] = refresh("open-webui", rt, secret=SECRETS["open-webui"])[0]
    out["disable_user"] = set_enabled(t, False)
    out["refresh_after_disable_refused"] = status(*refresh("open-webui", rt, secret=SECRETS["open-webui"]))
    out["enable_user"] = set_enabled(t, True)
    s, js = refresh("open-webui", rt, secret=SECRETS["open-webui"])
    out["old_offline_token_after_reenable"] = status(s, js)
    return out


def g_exchange(alice_sub):
    out = {}
    _, owui = password_grant("open-webui")
    base = {"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": owui["access_token"],
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token"}
    lite = ("litellm", SECRETS["litellm"])
    s, js = http("POST", TOKEN_URL, form={**base, "audience": "memmachine-platform"}, auth=lite)
    if s != 200:
        return {"to_platform": status(s, js)}
    c = claims(js["access_token"])
    out["to_platform"] = {"status": 200, "same_sub": c.get("sub") == alice_sub, "azp": c.get("azp"),
                          "aud": c.get("aud"), "act": "act" in c, "lifespan_s": c["exp"] - c["iat"],
                          "subject_lifespan_left_s": claims(owui["access_token"])["exp"] - int(time.time())}
    s, js = http("POST", TOKEN_URL, form={**base, "audience": "other-api"}, auth=lite)
    out["other_audience_refused"] = status(s, js, aud=claims(js["access_token"]).get("aud") if s == 200 else None)
    _, mm = code_flow("mm-auth", key=KEY1)                       # names the platform, not LiteLLM
    s, js = http("POST", TOKEN_URL, form={**base, "subject_token": mm.get("access_token", "x"),
                                          "audience": "memmachine-platform"}, auth=lite)
    out["subject_not_naming_litellm_refused"] = status(s, js)
    s, js = http("POST", TOKEN_URL, form={**base, "client_id": "litellm", "audience": "memmachine-platform"})
    out["no_client_secret_refused"] = status(s, js)
    s, cj = http("POST", TOKEN_URL, form={"grant_type": "client_credentials"}, auth=lite)
    s, js = http("POST", TOKEN_URL, form={**base, "subject_token": cj.get("access_token", "x"),
                                          "audience": "memmachine-platform"}, auth=lite)
    ec_ = claims(js["access_token"]) if s == 200 else {}
    out["own_service_token_as_subject"] = status(s, js, sub_is_alice=(ec_.get("sub") == alice_sub),
                                                 client_id_claim=("client_id" in ec_), clientId_claim=("clientId" in ec_),
                                                 preferred_username=ec_.get("preferred_username"), aud=ec_.get("aud"))
    # the same with LiteLLM's service account turned off: no own token to exchange, people's still work
    h = {"Authorization": f"Bearer {admin_token()}"}
    _, cl = http("GET", f"{BASE}/admin/realms/{REALM}/clients?clientId=litellm", headers=h)
    cl = cl[0]
    cl["serviceAccountsEnabled"] = False
    http("PUT", f"{BASE}/admin/realms/{REALM}/clients/{cl['id']}", body=cl, headers=h)
    _, owui2 = password_grant("open-webui")
    s, js = http("POST", TOKEN_URL, form={**base, "subject_token": owui2["access_token"],
                                          "audience": "memmachine-platform"}, auth=lite)
    s2, js2 = http("POST", TOKEN_URL, form={"grant_type": "client_credentials"}, auth=lite)
    out["service_account_off"] = {"people_exchange": status(s, js), "own_token_refused": status(s2, js2)}
    return out


def g_device():
    verifier, challenge = pkce()
    s, js = http("POST", f"{BASE}/realms/{REALM}/protocol/openid-connect/auth/device",
                 form={"client_id": "mm-auth", "code_challenge": challenge, "code_challenge_method": "S256"})
    return status(s, js, has_device_code=isinstance(js, dict) and "device_code" in js)


def safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        return {"exception": repr(e)[:200]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("version")
    ap.add_argument("--features", default="")
    ap.add_argument("--keep-image", action="store_true")
    a = ap.parse_args()
    rec = {"version": a.version, "features_flag": a.features or None, "ts": time.time()}
    try:
        ok, info = start(a.version, a.features)
        rec["started"] = ok
        if not ok:
            rec["start_error"] = info
            return
        rec["start_s"] = info
        t = admin_token()
        rec["server"] = safe(server_info, t)
        rec["policy_setup"] = safe(setup, t)
        rec["contract"] = safe(g_contract)
        sub = rec["contract"].pop("_sub", None) if isinstance(rec["contract"], dict) else None
        rec["dpop"] = safe(g_dpop)
        rec["exchange"] = safe(g_exchange, sub)
        rec["device"] = safe(g_device)
        rec["offline"] = safe(g_offline, admin_token())
    finally:
        stop()
        if not a.keep_image:
            docker("rmi", f"quay.io/keycloak/keycloak:{a.version}", check=False)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(json.dumps(rec, indent=1))


if __name__ == "__main__":
    main()
