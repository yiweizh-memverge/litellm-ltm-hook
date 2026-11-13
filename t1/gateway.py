"""Mock memmachine-gateway for T1: checks each user's Keycloak access token itself.

A stand-in for the token checks PR #1561 gives memmachine-gateway, with memory
kept in this process (no MemMachine core): T1 tests the token path, not recall.

    T1_GATEWAY_LOG=$T1_ENV/logs/gateway.jsonl python t1/gateway.py   # 127.0.0.1:18100

Token checks, in order (any failure: 401, X-MemMachine-Decision: refused):
  - a Bearer token; RS256 only; signature against the issuer's JWKS
  - iss is the configured issuer; exp not passed (30 s leeway)
  - typ is "Bearer": Keycloak's access tokens carry it, ID tokens carry "ID"
  - aud names the platform, if T1_REQUIRED_AUDIENCE is set (T4's exchange mode)
  - azp is a client named for the memories API (T1_ALLOWED_CLIENTS)
  - no client_id claim: Keycloak marks client-credentials tokens with it
  - a sub
Then the account: found by sub, never by email, recorded at its first request;
a disabled account is refused (403, account-disabled).

Memory API (bodies are JSON):
  POST /api/v2/memories/search  {"query": str}            -> {"episodes": [...], "binding": str}
  POST /api/v2/memories         {"messages": [...], "binding": str}
       a store is refused (409, turn-outdated) if its binding names another
       account, or the account's memory was erased since the search.
  POST /api/v2/memories/binding/check  {"binding": str}   -> {"binding": str}
       the re-check a hook makes when a turn's token changes (#1561, "The
       binding"): the same token checks and the same binding rule as a store,
       answered from the account table alone: no search, no memory read.

Test-only endpoints (no auth; this binds to 127.0.0.1):
  GET  /t1/state                 accounts, memory counts, refusal counts
  POST /t1/disable {"sub": ..}   POST /t1/enable {"sub": ..}
  POST /t1/erase   {"sub": ..}   (advances the account's generation)
  POST /t1/reset
Every decision is appended to T1_GATEWAY_LOG, with the token's claims and a
SHA-256 prefix, never the token.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from typing import Optional

import httpx
import jwt
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ISSUER = os.environ.get("T1_ISSUER", "http://127.0.0.1:18180/realms/corp")
ALLOWED_CLIENTS = set(os.environ.get("T1_ALLOWED_CLIENTS", "open-webui,mm-auth").split(","))
REQUIRED_AUDIENCE = os.environ.get("T1_REQUIRED_AUDIENCE", "")   # T4: "memmachine-platform"
LOG = os.environ.get("T1_GATEWAY_LOG", "gateway.jsonl")
PORT = int(os.environ.get("T1_GATEWAY_PORT", "18100"))
LEEWAY = 30
TOP_K = 5

_binding_key = secrets.token_bytes(32)
_jwks: Optional[jwt.PyJWKSet] = None
_jwks_at = 0.0

accounts: dict = {}     # sub -> {"id", "created", "disabled", "generation", "username"}
memory: dict = {}       # account id -> [{"text", "ts"}]
refusals: dict = {}     # reason -> count

app = FastAPI()


def _log(entry: dict) -> None:
    entry["ts"] = time.time()
    with open(LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _refuse(status: int, decision: str, reason: str, claims: Optional[dict] = None, path: str = ""):
    refusals[reason] = refusals.get(reason, 0) + 1
    _log({"path": path, "decision": decision, "reason": reason, "claims": claims})
    return JSONResponse({"decision": decision, "reason": reason}, status_code=status,
                        headers={"X-MemMachine-Decision": decision})


async def _signing_key(token: str):
    global _jwks, _jwks_at
    kid = jwt.get_unverified_header(token).get("kid")
    for attempt in (0, 1):
        if _jwks is None or attempt or time.time() - _jwks_at > 300:
            async with httpx.AsyncClient(timeout=10) as c:
                meta = (await c.get(f"{ISSUER}/.well-known/openid-configuration")).json()
                _jwks = jwt.PyJWKSet.from_dict((await c.get(meta["jwks_uri"])).json())
                _jwks_at = time.time()
        for k in _jwks.keys:
            if k.key_id == kid:
                return k.key
    raise jwt.InvalidTokenError(f"unknown kid {kid}")


def _safe_claims(token: str) -> dict:
    try:
        c = jwt.decode(token, options={"verify_signature": False})
    except Exception:
        return {"unparseable": True}
    out = {k: c.get(k) for k in ("iss", "sub", "azp", "aud", "typ", "exp", "preferred_username")}
    out["client_id_claim"] = "client_id" in c
    out["token_sha256_12"] = hashlib.sha256(token.encode()).hexdigest()[:12]
    return out


async def _authenticate(request: Request):
    """Return (account, claims) or a refusal response."""
    path = request.url.path
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or not auth[7:].strip():
        return None, _refuse(401, "refused", "no-bearer", path=path)
    token = auth[7:].strip()
    claims = _safe_claims(token)
    try:
        if jwt.get_unverified_header(token).get("alg") != "RS256":
            return None, _refuse(401, "refused", "alg-not-rs256", claims, path)
        # Name another issuer's token as such before looking for its key, which only
        # this issuer's key set would hold. The verified decode below checks iss again.
        if claims.get("iss") != ISSUER:
            return None, _refuse(401, "refused", "wrong-issuer", claims, path)
        c = jwt.decode(token, await _signing_key(token), algorithms=["RS256"], issuer=ISSUER,
                       leeway=LEEWAY, options={"verify_aud": False, "require": ["exp", "iss", "sub"]})
    except jwt.ExpiredSignatureError:
        return None, _refuse(401, "refused", "expired", claims, path)
    except jwt.InvalidIssuerError:
        return None, _refuse(401, "refused", "wrong-issuer", claims, path)
    except jwt.InvalidTokenError as e:
        return None, _refuse(401, "refused", f"invalid:{type(e).__name__}", claims, path)
    if c.get("typ") != "Bearer":
        return None, _refuse(401, "refused", f"not-access-token:{c.get('typ')}", claims, path)
    if REQUIRED_AUDIENCE:
        aud = c.get("aud")
        if REQUIRED_AUDIENCE not in (aud if isinstance(aud, list) else [aud]):
            return None, _refuse(401, "refused", f"audience-not-platform:{aud}", claims, path)
    if c.get("azp") not in ALLOWED_CLIENTS:
        return None, _refuse(401, "refused", f"client-not-allowed:{c.get('azp')}", claims, path)
    if "client_id" in c:
        return None, _refuse(401, "refused", "client-credentials-token", claims, path)

    acct = accounts.get(c["sub"])
    if acct is None:
        acct = {"id": str(uuid.uuid4()), "created": time.time(), "disabled": False,
                "generation": 0, "username": c.get("preferred_username")}
        accounts[c["sub"]] = acct
        _log({"path": path, "decision": "account-recorded", "claims": claims, "account": acct["id"]})
    if acct["disabled"]:
        return None, _refuse(403, "account-disabled", "account-disabled", claims, path)
    return (acct, claims), None


def _make_binding(acct: dict) -> str:
    raw = json.dumps({"a": acct["id"], "g": acct["generation"]}).encode()
    mac = hmac.new(_binding_key, raw, hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(raw).decode() + "." + mac


def _read_binding(binding: str) -> Optional[dict]:
    try:
        b64, mac = binding.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(b64)
        if not hmac.compare_digest(mac, hmac.new(_binding_key, raw, hashlib.sha256).hexdigest()[:32]):
            return None
        return json.loads(raw)
    except Exception:
        return None


@app.post("/api/v2/memories/search")
async def search(request: Request):
    who, refusal = await _authenticate(request)
    if refusal:
        return refusal
    acct, claims = who
    body = await request.json()
    episodes = [m["text"] for m in memory.get(acct["id"], [])][-TOP_K:]
    _log({"path": request.url.path, "decision": "served", "claims": claims, "account": acct["id"],
          "query_chars": len(body.get("query") or ""), "returned": len(episodes)})
    return {"episodes": episodes, "binding": _make_binding(acct)}


@app.post("/api/v2/memories")
async def store(request: Request):
    who, refusal = await _authenticate(request)
    if refusal:
        return refusal
    acct, claims = who
    body = await request.json()
    b = _read_binding(body.get("binding") or "")
    if b is None or b["a"] != acct["id"] or b["g"] != acct["generation"]:
        return _refuse(409, "turn-outdated", "binding-mismatch", claims, request.url.path)
    texts = [f'{m.get("role")}: {m.get("content")}' for m in body.get("messages") or []]
    memory.setdefault(acct["id"], []).append({"text": " | ".join(texts), "ts": time.time()})
    _log({"path": request.url.path, "decision": "stored", "claims": claims, "account": acct["id"]})
    return {"status": "ok"}


@app.post("/api/v2/memories/binding/check")
async def check_binding(request: Request):
    who, refusal = await _authenticate(request)
    if refusal:
        return refusal
    acct, claims = who
    body = await request.json()
    b = _read_binding(body.get("binding") or "")
    if b is None or b["a"] != acct["id"] or b["g"] != acct["generation"]:
        return _refuse(409, "turn-outdated", "binding-mismatch", claims, request.url.path)
    _log({"path": request.url.path, "decision": "rechecked", "claims": claims, "account": acct["id"]})
    return {"binding": _make_binding(acct)}


@app.get("/t1/state")
async def state():
    return {"accounts": {s: {**a, "memories": len(memory.get(a["id"], []))} for s, a in accounts.items()},
            "refusals": refusals}


async def _sub_action(request: Request, fn):
    sub = (await request.json())["sub"]
    if sub not in accounts:
        return JSONResponse({"error": "unknown sub"}, status_code=404)
    fn(accounts[sub])
    return accounts[sub]


@app.post("/t1/disable")
async def disable(request: Request):
    return await _sub_action(request, lambda a: a.update(disabled=True))


@app.post("/t1/enable")
async def enable(request: Request):
    return await _sub_action(request, lambda a: a.update(disabled=False))


@app.post("/t1/erase")
async def erase(request: Request):
    def _erase(a):
        memory.pop(a["id"], None)
        a["generation"] += 1
    return await _sub_action(request, _erase)


@app.post("/t1/reset")
async def reset():
    accounts.clear(), memory.clear(), refusals.clear()
    return {"status": "reset"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
