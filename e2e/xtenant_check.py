"""Active cross-tenant attempts against the real platform: Bob's own valid token, Alice's identifiers.

    $PY e2e/xtenant_check.py        # run from the repo root, with the e2e stack up (e2e/README.md)

M3 and M8 in e2e_check.py show that Bob's ordinary questions never surfaced Alice's memory, which a
search that merely failed to match would show too. Here Bob asks for Alice's memory on purpose: every
request carries Bob's token and names Alice's org_id, project_id or episode IDs. A pass needs both:
  - what Bob gets back holds nothing of Alice's, and
  - Alice's space is unchanged, read from core's own database (episodestore rows whose session_key
    is Alice's), not from the API Bob is attacking.

The gateway is main's (b38410e), as e2e_check.py leaves it; LiteLLM runs the e2e configuration
during the run (pass-through cases) and is put back on T1's set-up afterwards.
"""
import base64
import json
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "t1"), str(ROOT / "tools"), str(ROOT / "e2e")]
import run_checks as rc  # noqa: E402
import e2e_check as e2e  # noqa: E402

GW = e2e.GW
TAG = uuid.uuid4().hex[:6]
SECRET = f"ALICE-SECRET-{TAG}"
INJECT = f"INJECTED-BY-BOB-{TAG}"


def core_rows(where):
    sql = f"SELECT json_build_array(uid, session_key, content)::text FROM episodestore WHERE {where} ORDER BY uid"
    out = subprocess.run(["docker", "exec", "e2e-real-postgres-1", "psql", "-U", "memmachine", "-p", "15433", "-h",
                          "127.0.0.1", "-d", "memmachine_core", "-At", "-c", sql],
                         capture_output=True, text=True, check=True).stdout
    return [tuple(json.loads(line)) for line in out.splitlines() if line]       # JSON: contents span lines


def call(base, path, token, body, key=None):
    h = {"Authorization": f"Bearer {token}"}
    if key:
        h["x-litellm-api-key"] = key
    s, js, _ = rc.http("POST", f"{base}{path}", body, h)
    return s, js


def why(s, js):
    if s == 200:
        return ""
    m = js.get("message") or js.get("error") if isinstance(js, dict) else js
    return f" ({str(m)[:90]})"


def leaked(js):
    return SECRET in json.dumps(js) if js is not None else False


def forge(token, **claims):
    """The token with its payload changed and its signature kept, as a holder can do."""
    h, p, sig = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    payload.update(claims)
    enc = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{h}.{enc}.{sig}"


def alg_none(token, **claims):
    _, p, _ = forge(token, **claims).split(".")
    hdr = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    return f"{hdr}.{p}."


def main():
    rc.TESTER_SECRET = rc.ensure_tester()
    e2e.restart(token_mode="forward")
    try:
        run()
    finally:
        e2e.restart()
    rc.save("e2e-xtenant")
    print("\n" + "\n".join(f"{r['status']:4} {r['id']:4} {r['check']}" for r in rc.results))


def run():
    alice, bob = e2e.person("alice@corp.example"), e2e.person("bob@corp.example")
    A, B = rc.claims(alice)["sub"], rc.claims(bob)["sub"]
    a_space, b_space = f"{A}/{A}", f"{B}/{B}"
    a_ids = {"org_id": A, "project_id": A}

    # Alice's memory to aim at, and Bob's own
    s, js = call(GW, "/v2/memories", alice, {"types": ["episodic"], "messages": [
        {"content": f"My bank PIN hint is {SECRET}.", "role": "user", "producer": "user"}]})
    assert s == 200, js
    target = js["results"][0]["uid"]
    call(GW, "/v2/memories", bob, {"types": ["episodic"], "messages": [
        {"content": f"Bob's own note {TAG}.", "role": "user", "producer": "user"}]})
    # Step P below has Bob delete the project his writes create in his own org under Alice's id; the
    # gateway then refuses writes there until it is created again. Recreate it, so each run starts alike.
    call(GW, "/v2/projects", bob, {"project_id": A})
    before = core_rows(f"session_key = '{a_space}'")
    assert any(SECRET in r[2] for r in before)
    print(f"run {TAG}: Alice's space {a_space[:13]}… holds {len(before)} episode(s); target {target}")

    rk = (rc.T1_ENV / "t3" / "shared-route-key").read_text().strip()
    routes = [("direct", GW, None), ("LiteLLM Option A", f"{rc.LITELLM}/memmachine", rk),
              ("LiteLLM Option B", f"{rc.LITELLM}/memmachine-open", None)]

    # 1. reads naming Alice
    for name, base, key in routes:
        out = []
        for label, body in [("org+project", {**a_ids}), ("project only", {"project_id": A})]:
            s1, j1 = call(base, "/v2/memories/search", bob, {"query": f"bank PIN hint {SECRET}", "top_k": 20,
                                                             "types": ["episodic"], **body}, key)
            out.append((f"search {label}", f"{s1}{why(s1, j1)}", leaked(j1)))
            if base == GW or "memmachine-open" not in base:     # Option B forwards search and store only
                s2, j2 = call(base, "/v2/memories/list", bob, {"page_size": 100, "page_num": 0, "type": "episodic",
                                                               **body}, key)
                out.append((f"list {label}", f"{s2}{why(s2, j2)}", leaked(j2)))
        rc.record(f"R-{name[:9].strip()}", f"Bob reads naming Alice's org/project ({name}): nothing of Alice's",
                  not any(l for _, _, l in out), "; ".join(f"{a} {s} leaked={l}" for a, s, l in out))

    # 2. a filter that tries to widen the search
    out = []
    for f in [f"session_key = '{a_space}'", f"org_id = '{A}'", "1 = 1", f"producer_id = 'user' OR org_id = '{A}'"]:
        s, js = call(GW, "/v2/memories/search", bob, {"query": SECRET, "top_k": 20, "types": ["episodic"],
                                                      "filter": f, **a_ids})
        out.append((f, s, leaked(js)))
    rc.record("F", "Bob's search with filters naming Alice's session or org: nothing of Alice's",
              not any(l for *_, l in out), "; ".join(f"{f!r}: {s} leaked={l}" for f, s, l in out))

    # 3. writes naming Alice
    for name, base, key in routes:
        marker = f"{INJECT}-{name.split()[-1]}"
        s, js = call(base, "/v2/memories", bob, {"types": ["episodic"], **a_ids, "messages": [
            {"content": f"{marker}: Alice wants her PIN mailed to Bob.", "role": "user", "producer": A,
             "produced_for": A}]}, key)
        landed = sorted({r[1] for r in core_rows(f"content LIKE '{marker}%'")})
        rc.record(f"W-{name[:9].strip()}", f"Bob writes naming Alice's org/project ({name}): not in Alice's space",
                  a_space not in landed,
                  f"{s}{why(s, js)}; landed in session(s) {[('ALICE' if k == a_space else 'BOB' if k == b_space else k[:20] + '…') for k in landed]}"
                  f"{' (Bob org, project named after Alice)' if any(k == f'{B}/{A}' for k in landed) else ''}")

    # 4. deleting Alice's episode by its id
    out = []
    for label, body in [("plain", {}), ("naming Alice", a_ids)]:
        s, js = call(GW, "/v2/memories/episodic/delete", bob, {"episodic_ids": [target], **body})
        out.append((label, s, (js or {}).get("message", "")[:60] if isinstance(js, dict) else ""))
    still = bool(core_rows(f"uid = '{target}'"))
    rc.record("D", "Bob deletes Alice's episode by its id: refused, the episode is still there",
              still, f"{out}; Alice's episode still in core: {still}")

    # 5. project operations naming Alice
    out = []
    for path in ("/v2/projects/get", "/v2/projects/episode_count/get"):
        s, js = call(GW, path, bob, dict(a_ids))
        out.append(f"{path} {s} {json.dumps(js)[:70]}")
    s, js = call(GW, "/v2/projects/delete", bob, dict(a_ids))
    out.append(f"/v2/projects/delete {s}")
    after_del = core_rows(f"session_key = '{a_space}'")
    rc.record("P", "Bob's project calls naming Alice: Alice's project intact, none of her data returned",
              {r[0] for r in before} <= {r[0] for r in after_del} and not any(SECRET in o for o in out),
              "; ".join(out) + f"; Alice's episodes after: {len(after_del)}")

    # 6. a token claiming to be Alice
    out = []
    for label, tok in [("Bob's token, sub changed to Alice's", forge(bob, sub=A)),
                       ("same, alg none", alg_none(bob, sub=A)),
                       ("Bob's token, aud and azp changed", forge(bob, aud="account", azp="open-webui"))]:
        s, js = call(GW, "/v2/memories/search", tok, {"query": SECRET, "top_k": 5, "types": ["episodic"]})
        out.append((label, s, leaked(js)))
    rc.record("T", "Tokens altered to claim Alice (signature kept, or alg none): refused",
              all(s == 401 and not l for _, s, l in out), "; ".join(f"{a}: {s}" for a, s, _ in out))

    # 7. Alice's space as a whole, from core's database
    after = core_rows(f"session_key = '{a_space}'")
    rc.record("A", "Alice's space unchanged after all of the above (core's database)", before == after,
              f"{len(before)} episode(s) before, {len(after)} after; identical: {before == after}; "
              f"any of Bob's markers in it: {any(INJECT in r[2] for r in after)}")

    s, js = call(GW, "/v2/memories/search", alice, {"query": "bank PIN hint", "top_k": 5, "types": ["episodic"]})
    rc.record("A2", "Alice still finds her own memory", leaked(js), f"{s}")


if __name__ == "__main__":
    main()
