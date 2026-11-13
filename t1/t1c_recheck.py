"""T1c: a token that changes mid-turn must pass a binding re-check before it gets the turn's memory.

#1561's hook, for "a later request with a new token": re-check the turn's binding with the new
token; if the platform accepts it, add the first result back, now bound to the new token;
otherwise add no memory with that token for the rest of the turn. Each case is one turn of two or
three requests sent to LiteLLM as Open WebUI sends them (one x-openwebui-message-id, a tool round
in between), with something changed before the second request:

  R1 same person, new token          re-check accepted, memory added, store accepted
  R2 another person's token          re-check refused, no memory (twice, re-checked once), store refused
  R3 the person's memory erased      re-check refused, no memory, store refused
  R4 the person disabled             re-check refused, no memory, store refused
  R5 same token                      no re-check, memory added, store accepted

In every case the re-check is not a search: the gateway serves one search per turn, the first.
Tokens come from the t1-tester client (10 s lifetime), alice's and bob's.

    $PY t1/t1c_recheck.py        # after t1/svc.sh restart gateway litellm
"""
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_checks as rc  # noqa: E402

TOOLS = [{"type": "function", "function": {"name": "lookup_fact", "description": "t1 tool",
                                           "parameters": {"type": "object", "properties": {}}}}]
BLOCK = "Memory (quoted data"


def has_memory(provider_entry):
    msgs = (provider_entry.get("body") or {}).get("messages") or []
    return any(m.get("role") == "system" and str(m.get("content", "")).startswith(BLOCK) for m in msgs)


def send(token, messages, mid):
    """One request of the turn; returns its status and whether the provider got the memory block."""
    t = time.time()
    s, js, _ = rc.litellm_chat(token, messages, {"x-openwebui-message-id": mid, "x-openwebui-task": "",
                                                 "x-openwebui-model-id": rc.MODEL}, TOOLS)
    time.sleep(0.5)
    prov = [e for e in rc.log_since("provider.jsonl", t) if e.get("path", "").endswith("/chat/completions")]
    return s, js, (has_memory(prov[-1]) if prov else None)


def gateway(path, sub):
    return rc.http("POST", f"{rc.GATEWAY}/t1/{path}", {"sub": sub})[0]


def seed(email):
    """Give the person a memory to be added, whatever the gateway held before."""
    tok = rc.mint(email)["access_token"]
    mid = str(uuid.uuid4())
    rc.litellm_chat(tok, [{"role": "user", "content": f"t1c seed for {email}"}],
                    {"x-openwebui-message-id": mid, "x-openwebui-task": "", "x-openwebui-model-id": rc.MODEL})
    time.sleep(1)


def case(cid, name, second_email, between=None, after=None, third=False, same_token=False, want=None):
    alice_sub = rc.claims(rc.mint("alice@corp.example")["access_token"])["sub"]
    mid, t0 = str(uuid.uuid4()), time.time()
    tok1 = rc.mint("alice@corp.example")["access_token"]
    user = {"role": "user", "content": f"[tool] t1c {cid}"}
    s1, r1, mem1 = send(tok1, [user], mid)
    call = r1["choices"][0]["message"]["tool_calls"][0]
    if between:
        between(alice_sub)
    tok2 = tok1 if same_token else rc.mint(second_email)["access_token"]
    round1 = [user, {"role": "assistant", "content": None, "tool_calls": [call]},
              {"role": "tool", "tool_call_id": call["id"], "content": "42"}]
    mems = [mem1]
    if third:                                   # one more tool round with the same second token
        more = {"role": "user", "content": "[tool] t1c again"}
        s2, r2, mem2 = send(tok2, round1 + [more], mid)
        mems.append(mem2)
        call2 = r2["choices"][0]["message"]["tool_calls"][0]
        round1 = round1 + [more, {"role": "assistant", "content": None, "tool_calls": [call2]},
                           {"role": "tool", "tool_call_id": call2["id"], "content": "43"}]
    s3, _, mem3 = send(tok2, round1, mid)
    mems.append(mem3)
    time.sleep(1)
    if after:
        after(alice_sub)
    hook = [e for e in rc.log_since("hook.jsonl", t0) if e.get("turn") == mid]
    ev = [e["event"] for e in hook]
    rechecks = [(e["status"], e["decision"]) for e in hook if e["event"] == "recheck"]
    store = next(((e["status"], e["decision"]) for e in hook if e["event"] == "store"), None)
    served = [e for e in rc.log_since("gateway.jsonl", t0)
              if e.get("path") == "/api/v2/memories/search" and e.get("decision") == "served"]
    got = {"memory_by_request": mems, "rechecks": rechecks, "store": store, "searches_served": len(served)}
    ok = (mems == want["memory"] and [r[0] for r in rechecks] == want["rechecks"]
          and (store or (None,))[0] == want["store"] and len(served) == 1)
    rc.record(cid, name, ok, f"{got}; hook events {ev}")


def main():
    rc.TESTER_SECRET = rc.ensure_tester()
    seed("alice@corp.example")
    case("R1", "same person, new token: re-checked and accepted before the memory is added",
         "alice@corp.example", want={"memory": [True, True], "rechecks": [200], "store": 200})
    case("R2", "another person's token: re-check refused, no memory for it, store refused",
         "bob@corp.example", third=True,
         want={"memory": [True, False, False], "rechecks": [409], "store": 409})
    case("R3", "memory erased mid-turn: re-check refused, no memory, store refused",
         "alice@corp.example", between=lambda sub: gateway("erase", sub),
         want={"memory": [True, False], "rechecks": [409], "store": 409})
    seed("alice@corp.example")
    case("R4", "person disabled mid-turn: re-check refused, no memory, store refused",
         "alice@corp.example", between=lambda sub: gateway("disable", sub), after=lambda sub: gateway("enable", sub),
         want={"memory": [True, False], "rechecks": [403], "store": 403})
    case("R5", "same token: no re-check, memory added again, store accepted",
         "alice@corp.example", same_token=True, want={"memory": [True, True], "rechecks": [], "store": 200})
    rc.save("t1c")
    print("\n" + "\n".join(f"{r['status']:4} {r['id']:3} {r['check']}" for r in rc.results))


if __name__ == "__main__":
    main()
