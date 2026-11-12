"""A fake OpenAI-compatible model provider for T1 that records everything it is sent.

LiteLLM's model points its api_base here, so every header and body that reaches
"the model provider" lands in a JSONL file. The leak checks (A8, A9) search that
file for the user's Keycloak token.

    FAKE_PROVIDER_LOG=$T1_ENV/logs/provider.jsonl \
      python t1/fake_provider.py            # listens on 127.0.0.1:18200

Replies:
  - a request offering tools whose last user message contains "[tool]" gets one
    call to the first tool (for the tool-round check, A5);
  - everything else gets "OK: <last user message>", streamed if asked.
"""
import json
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

LOG = os.environ.get("FAKE_PROVIDER_LOG", "provider.jsonl")
PORT = int(os.environ.get("FAKE_PROVIDER_PORT", "18200"))
MODEL = "fake-model"

app = FastAPI()


def _record(request: Request, body: dict) -> None:
    with open(LOG, "a") as f:
        f.write(json.dumps({
            "ts": time.time(),
            "path": request.url.path,
            "headers": dict(request.headers),
            "body": body,
        }) + "\n")


def _last_user_text(messages: list) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return str(c or "")
    return ""


@app.get("/v1/models")
async def models(request: Request):
    _record(request, {})
    return {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "t1"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    _record(request, body)
    messages = body.get("messages") or []
    tools = body.get("tools") or []
    last_is_user = bool(messages) and messages[-1].get("role") == "user"
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if tools and last_is_user and "[tool]" in _last_user_text(messages):
        fn = tools[0].get("function", {}).get("name", "tool")
        call = {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                "function": {"name": fn, "arguments": "{}"}}
        message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
    else:
        mem = [m for m in messages if m.get("role") == "system"
               and str(m.get("content", "")).startswith("Memory (quoted data")]
        lines = [l[2:] for m in mem for l in str(m["content"]).splitlines() if l.startswith("> ")]
        note = f"\n\n[memory injected: {len(lines)} item(s)]" + "".join(f"\n- {l[:120]}" for l in lines)
        message, finish = {"role": "assistant", "content": f"OK: {_last_user_text(messages)[:200]}{note}"}, "stop"

    if not body.get("stream"):
        return JSONResponse({
            "id": cid, "object": "chat.completion", "created": created, "model": MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    def stream():
        delta = dict(message)
        if delta.get("tool_calls"):
            delta["tool_calls"] = [{**delta["tool_calls"][0], "index": 0}]
        for d, fin in ((delta, None), ({}, finish)):
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": MODEL,
                     "choices": [{"index": 0, "delta": d, "finish_reason": fin}]}
            yield f"data: {json.dumps(chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
