"""T3 setup: mm-auth's client in the T1 realm, as #1561's contract asks for it.

    $PY t3/t3_setup.py [--lifespan 60]

- mm-auth: a public client, PKCE (S256), the standard code flow back to a loopback port
  (http://127.0.0.1/*; Keycloak ignores the port of a loopback redirect, RFC 8252 §7.3),
  offline_access, the device grant on, the platform as audience, no direct grants.
- Its refresh token bound to mm-auth's key by DPoP, the access token left a bearer: a client
  policy with dpop-bind-enforcer, allow-only-refresh-token-binding, auto-configure off, applied
  to clients holding the client role "dpop-bound" (as in T2's matrix).
- Its access tokens live --lifespan seconds (short, so the checks see refreshes).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "t1"))
import run_checks as rc  # noqa: E402

CLIENT = "mm-auth"
PROFILE, POLICY, ROLE = "mm-auth-dpop", "mm-auth-dpop", "dpop-bound"


def aud_mapper(client):
    return {"name": f"aud-{client}", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
            "config": {"included.client.audience": client, "access.token.claim": "true", "id.token.claim": "false"}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lifespan", type=int, default=60)
    a = ap.parse_args()
    t = rc.kc_admin()
    h = {"Authorization": f"Bearer {t}"}
    base = f"{rc.KC}/admin/realms/{rc.REALM}"
    rep = {"clientId": CLIENT, "enabled": True, "protocol": "openid-connect", "publicClient": True,
           "standardFlowEnabled": True, "directAccessGrantsEnabled": False, "implicitFlowEnabled": False,
           "serviceAccountsEnabled": False, "redirectUris": ["http://127.0.0.1/*"], "webOrigins": [],
           "attributes": {"pkce.code.challenge.method": "S256",
                          "oauth2.device.authorization.grant.enabled": "true",
                          "access.token.lifespan": str(a.lifespan)}}
    c = rc.kc_client(t, CLIENT)
    if c is None:
        assert rc.http("POST", f"{base}/clients", {**rep, "protocolMappers": [aud_mapper("memmachine-platform")]}, h)[0] == 201
        c = rc.kc_client(t, CLIENT)
    else:
        c.update({k: v for k, v in rep.items() if k != "attributes"})
        c["attributes"] = {**(c.get("attributes") or {}), **rep["attributes"]}
        assert rc.http("PUT", f"{base}/clients/{c['id']}", c, h)[0] == 204
    if rc.http("GET", f"{base}/clients/{c['id']}/roles/{ROLE}", headers=h)[0] == 404:
        assert rc.http("POST", f"{base}/clients/{c['id']}/roles", {"name": ROLE}, h)[0] == 201

    _, profs, _ = rc.http("GET", f"{base}/client-policies/profiles", headers=h)
    profs = [p for p in (profs or {}).get("profiles", []) if p["name"] != PROFILE]
    profs.append({"name": PROFILE, "description": "bind mm-auth's refresh token, not its access token",
                  "executors": [{"executor": "dpop-bind-enforcer", "configuration": {
                      "auto-configure": "false", "allow-only-refresh-token-binding": "true"}}]})
    s, js, _ = rc.http("PUT", f"{base}/client-policies/profiles", {"profiles": profs}, h)
    assert s < 300, (s, js)
    _, pols, _ = rc.http("GET", f"{base}/client-policies/policies", headers=h)
    pols = [p for p in (pols or {}).get("policies", []) if p["name"] != POLICY]
    pols.append({"name": POLICY, "enabled": True,
                 "conditions": [{"condition": "client-roles", "configuration": {"roles": [ROLE]}}],
                 "profiles": [PROFILE]})
    s, js, _ = rc.http("PUT", f"{base}/client-policies/policies", {"policies": pols}, h)
    assert s < 300, (s, js)
    print(f"{CLIENT}: ready, access tokens {a.lifespan} s, refresh token DPoP-bound")


if __name__ == "__main__":
    main()
