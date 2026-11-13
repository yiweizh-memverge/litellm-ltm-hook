"""T1b: one real Open WebUI turn with streaming on, and one through a real tool server.

Open WebUI runs tools (native function calling) only for a browser-like session: a socket.io
connection whose session ID the chat request carries. Without one it passes the model's answer
through untouched. So this script signs alice in, connects to Open WebUI's socket.io as the chat
page does, registers t1/tool_server.py as a tool server, and sends two prompts with streaming on:

  plain   no tools: one model request, streamed
  tool    the tool server offered: the model calls lookup_fact, Open WebUI calls the tool
          server and sends the result back, the model answers: two model requests, one turn

For each turn it checks: every model request streamed and carried the turn's message ID; the
tool server was called once (tool turn); the hook searched once and stored once; the stored
memory is the user's prompt and the final answer, exactly as the chat page received it.

    $PY t1/t1b_stream_tool.py         # needs t1/svc.sh start all (tools included)

alice is Open WebUI's admin: a tool server with no access grants is admin-only.
Results: $T1_ENV/logs/results-<version>-t1b.json.
"""
import asyncio
import sys
import time
import uuid
from pathlib import Path

import socketio

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_checks as rc  # noqa: E402

TOOL_SERVER = {"url": "http://127.0.0.1:18300", "path": "openapi.json", "type": "openapi", "auth_type": "none",
               "key": "", "config": {"enable": True},
               "info": {"id": "t1tools", "name": "T1 tools", "description": "T1b tool server"}}
TOOL_ID = "server:t1tools"


def tool_servers(tok, connections=None):
    h = {"Authorization": f"Bearer {tok}"}
    if connections is None:
        return rc.http("GET", f"{rc.OWUI}/api/v1/configs/tool_servers", headers=h)[1]["TOOL_SERVER_CONNECTIONS"]
    s, js, _ = rc.http("POST", f"{rc.OWUI}/api/v1/configs/tool_servers",
                       {"TOOL_SERVER_CONNECTIONS": connections}, h)
    assert s == 200, (s, js)


async def turn(session, text, tool_ids):
    """One prompt as the chat page sends it; returns the turn's message ID and what the page received."""
    opener, tok = session
    sio = socketio.AsyncClient()
    events = []
    done = asyncio.Event()

    @sio.on("events")
    async def on_event(data):
        events.append(data)
        d = (data or {}).get("data") or {}
        if d.get("type") == "chat:completion" and (d.get("data") or {}).get("done"):
            done.set()

    await sio.connect(rc.OWUI, socketio_path="/ws/socket.io", auth={"token": tok}, transports=["websocket"])
    await sio.emit("user-join", {"auth": {"token": tok}})
    await asyncio.sleep(0.5)
    mid, umid = str(uuid.uuid4()), str(uuid.uuid4())
    body = {"model": rc.MODEL, "stream": True, "chat_id": f"local:{uuid.uuid4()}", "id": mid,
            "session_id": sio.sid, "messages": [{"role": "user", "content": text}],
            "user_message": {"id": umid, "role": "user", "content": text},
            "tool_ids": tool_ids,
            "background_tasks": {"title_generation": False, "tags_generation": False,
                                 "follow_up_generation": False}}
    s, js, _ = await asyncio.to_thread(rc.http, "POST", f"{rc.OWUI}/api/chat/completions", body,
                                       {"Authorization": f"Bearer {tok}"}, opener=opener, timeout=120)
    try:
        await asyncio.wait_for(done.wait(), 90)
    except asyncio.TimeoutError:
        pass
    await sio.disconnect()
    # what the page shows: text deltas as they stream, then the finished output's last message
    deltas, final = [], None
    for e in events:
        d = (e or {}).get("data") or {}
        x = d.get("data") if isinstance(d.get("data"), dict) else {}
        if d.get("type") == "response:completion" and x.get("type") == "response.output_text.delta":
            deltas.append(x.get("delta") or "")
        if d.get("type") == "chat:completion" and x.get("done"):
            msgs = [o for o in x.get("output") or [] if o.get("type") == "message"]
            if msgs:
                final = "".join(c.get("text", "") for c in msgs[-1].get("content") or [])
    kinds = [o.get("type") for e in events for o in (((e or {}).get("data") or {}).get("data") or {}).get("output") or []
             if (((e or {}).get("data") or {}).get("data") or {}).get("done")]
    return mid, {"http": s, "events": len(events), "done": done.is_set(), "final": final,
                 "streamed": "".join(deltas), "delta_events": len(deltas), "output_items": kinds}


def check(label, mid, page, t0, want_tool):
    time.sleep(2)
    # LiteLLM does not pass Open WebUI's headers on to the provider; the turns run one at a time
    # with background tasks off, so the turn's model requests are the provider's requests since t0
    prov = [e for e in rc.log_since("provider.jsonl", t0) if e.get("path", "").endswith("/chat/completions")]
    tools = rc.log_since("tool.jsonl", t0)
    hook = [e for e in rc.log_since("hook.jsonl", t0) if e.get("turn") == mid]
    ev = [e["event"] for e in hook]
    stores = [e for e in hook if e["event"] == "store"]
    reqs = [{"stream": (e.get("body") or {}).get("stream"),
             "tools_offered": len((e.get("body") or {}).get("tools") or []),
             "last_role": ((e.get("body") or {}).get("messages") or [{}])[-1].get("role")} for e in prov]
    rc.record("T1b", f"{label}: the page got the answer streamed, deltas adding up to the final text",
              page["done"] and bool(page["final"]) and page["delta_events"] > 0 and page["streamed"] == page["final"],
              f"done={page['done']} deltas={page['delta_events']} output items={page['output_items']}")
    rc.record("T1b", f"{label}: every model request streamed, one turn",
              bool(prov) and all(r["stream"] for r in reqs) and len(prov) == (2 if want_tool else 1),
              f"model requests for the turn: {reqs}")
    if want_tool:
        rc.record("T1b", f"{label}: the tool server was called once, by Open WebUI", len(tools) == 1,
                  f"tool calls: {[{k: t[k] for k in ('q', 'authorization')} for t in tools]}")
        rc.record("T1b", f"{label}: second request carries the tool result",
                  len(reqs) == 2 and reqs[1]["last_role"] == "tool", f"{reqs}")
    rc.record("T1b", f"{label}: one search, one store", ev.count("search") == 1 and ev.count("store") == 1
              and all(s.get("status") == 200 for s in stores), f"hook events for the turn: {ev}")
    return stores


def stored_is(label, prompt, final):
    """The newest memory alice has: the prompt and the answer the page received, nothing else."""
    tok = rc.mint("alice@corp.example")["access_token"]
    s, js, _ = rc.http("POST", f"{rc.GATEWAY}/api/v2/memories/search", {"query": "t1b"},
                       {"Authorization": f"Bearer {tok}"})
    if s == 401 and (rc.T1_ENV / "exchange.env").exists():     # gateway in T4's exchange mode
        sec = dict(l.split("=", 1) for l in (rc.T1_ENV / "exchange.env").read_text().split())
        _, x, _ = rc.http("POST", f"{rc.KC}/realms/{rc.REALM}/protocol/openid-connect/token", form={
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": tok,
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token", "audience": "memmachine-platform"},
            headers={"Authorization": "Basic " + rc.base64.b64encode(
                f"litellm:{sec['T1_EXCHANGE_CLIENT_SECRET']}".encode()).decode()})
        s, js, _ = rc.http("POST", f"{rc.GATEWAY}/api/v2/memories/search", {"query": "t1b"},
                           {"Authorization": f"Bearer {x['access_token']}"})
    newest = (js or {}).get("episodes", [None])[-1]
    want = f"user: {prompt} | assistant: {final}"
    rc.record("T1b", f"{label}: stored memory = prompt + the answer the page received", newest == want,
              f"stored {newest!r:.160}" if newest != want else "exact match")


async def main():
    rc.TESTER_SECRET = rc.ensure_tester()
    alice = rc.owui_session("alice@corp.example")
    before = tool_servers(alice[1])
    tool_servers(alice[1], [c for c in before if (c.get("info") or {}).get("id") != "t1tools"] + [TOOL_SERVER])
    rc.http("GET", f"{rc.OWUI}/api/models?refresh=true", headers={"Authorization": f"Bearer {alice[1]}"}, timeout=120)
    try:
        tag = uuid.uuid4().hex[:6]
        prompt = f"t1b plain {tag}: tell me something"
        t0 = time.time()
        mid, page = await turn(alice, prompt, [])
        check("plain streaming turn", mid, page, t0, want_tool=False)
        stored_is("plain streaming turn", prompt, page["final"])

        prompt = f"[tool] t1b tool {tag}: look the fact up"
        t0 = time.time()
        mid, page = await turn(alice, prompt, [TOOL_ID])
        check("tool turn", mid, page, t0, want_tool=True)
        stored_is("tool turn", prompt, page["final"])
    finally:
        tool_servers(alice[1], before)
    rc.save("t1b")
    print("\n" + "\n".join(f"{r['status']:4} {r['check']}" for r in rc.results))


if __name__ == "__main__":
    asyncio.run(main())
