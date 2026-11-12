"""Step-1 probe: what does LiteLLM's callback see of the user's token?

A LiteLLM callback that changes nothing. For every request it appends one line
to $T1_PROBE_LOG with the header names Open WebUI sent, the x-openwebui-* values,
and, from data["secret_fields"]["raw_headers"], the Authorization token's
unverified claims (sub, azp, typ, exp, iss) plus a SHA-256 prefix of the token.
It never writes the token itself.

    litellm_settings:
      callbacks:
        - t1_probe.probe
"""
import base64
import hashlib
import json
import os
import time

from litellm.integrations.custom_logger import CustomLogger

LOG = os.environ.get("T1_PROBE_LOG", "probe.jsonl")


def _claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        c = json.loads(base64.urlsafe_b64decode(part))
        return {k: c.get(k) for k in ("iss", "sub", "azp", "typ", "exp", "scope", "preferred_username")}
    except Exception as e:  # not a JWT
        return {"error": type(e).__name__}


class Probe(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        raw = (data.get("secret_fields") or {}).get("raw_headers") or {}
        raw = {k.lower(): v for k, v in dict(raw).items()}
        auth = raw.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else ""
        line = {
            "ts": time.time(),
            "call_type": call_type,
            "model": data.get("model"),
            "secret_fields_present": "secret_fields" in data,
            "header_names": sorted(raw),
            "owui": {k: v for k, v in raw.items() if k.startswith("x-openwebui-")},
            "litellm_key_owner": getattr(user_api_key_dict, "user_id", None),
            "token_sha256_12": hashlib.sha256(token.encode()).hexdigest()[:12] if token else None,
            "token_claims": _claims(token) if token else None,
        }
        with open(LOG, "a") as f:
            f.write(json.dumps(line) + "\n")
        return data


probe = Probe()
