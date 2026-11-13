"""A scripted browser for mm-auth login: signs a test user in on Keycloak's page and follows the
redirect to mm-auth's loopback port, as a person's browser would.

    MM_AUTH_BROWSER="$PY t3/fake_browser.py alice@corp.example" mm-auth login
"""
import html
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "t1"), str(ROOT / "tools")]
import run_checks as rc  # noqa: E402
from sso_browser import _LocalhostJar  # noqa: E402  (Keycloak's Secure cookies over http://127.0.0.1)

email, url = sys.argv[1], sys.argv[2]
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_LocalhostJar()))
page = opener.open(url, timeout=30).read().decode()
action = html.unescape(re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', page).group(1))
r = opener.open(urllib.request.Request(action, data=urllib.parse.urlencode(
    {"username": email, "password": rc.accounts()[email].strip(), "credentialId": ""}).encode()), timeout=30)
print(f"fake browser: landed on {urllib.parse.urlparse(r.geturl()).netloc}{urllib.parse.urlparse(r.geturl()).path}",
      file=sys.stderr)
