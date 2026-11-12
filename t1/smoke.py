"""Step-1 smoke test: sign in to Open WebUI through Keycloak and send one chat.

Runs as a browser would: the Keycloak login sets Open WebUI's cookies
(token, oauth_session_id), and the chat goes through Open WebUI's own API with
those cookies. Prints what the LiteLLM probe saw for that request.

    T1_ENV=~/t1-tokenpath-env python t1/smoke.py alice@corp.example "hello"
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from sso_browser import sso_login  # noqa: E402

T1_ENV = Path(os.environ.get("T1_ENV", Path.home() / "t1-tokenpath-env"))
OWUI = "http://127.0.0.1:18087"
MODEL = os.environ.get("T1_MODEL", "memmachine-fake")


def accounts():
    out = {}
    for line in (T1_ENV / "accounts.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            email, pw = line.split(None, 1)
            out[email] = pw.strip()
    return out


def cookie(opener, name):
    for h in opener.handlers:
        jar = getattr(h, "cookiejar", None)
        if jar is not None:
            for c in jar:
                if c.name == name:
                    return c.value
    return None


def main():
    email = sys.argv[1] if len(sys.argv) > 1 else "alice@corp.example"
    text = sys.argv[2] if len(sys.argv) > 2 else "hello from t1"
    opener, _ = sso_login(f"{OWUI}/oauth/oidc/login", email, accounts()[email])
    token = cookie(opener, "token")
    print(f"signed in as {email}; cookies: token={'yes' if token else 'no'}, "
          f"oauth_session_id={'yes' if cookie(opener, 'oauth_session_id') else 'no'}")

    probe = T1_ENV / "logs" / "probe.jsonl"
    before = probe.stat().st_size if probe.exists() else 0
    req = urllib.request.Request(
        f"{OWUI}/api/chat/completions",
        data=json.dumps({"model": MODEL, "stream": False,
                         "messages": [{"role": "user", "content": text}]}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST")
    with opener.open(req, timeout=120) as r:
        body = json.load(r)
    print("answer:", body["choices"][0]["message"]["content"])

    time.sleep(1)
    with open(probe) as f:
        f.seek(before)
        for line in f:
            print("probe saw:", json.dumps(json.loads(line), indent=1))


if __name__ == "__main__":
    main()
