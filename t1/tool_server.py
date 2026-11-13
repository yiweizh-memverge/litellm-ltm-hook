"""A minimal OpenAPI tool server for T1b: one tool, every call logged.

Open WebUI loads /openapi.json and offers its operations to the model as tools;
when the model calls one, Open WebUI's server calls this endpoint and sends the
result back to the model as a tool round of the same turn.

    T1_TOOL_LOG=$T1_ENV/logs/tool.jsonl python t1/tool_server.py   # 127.0.0.1:18300

The log records header names only (and whether an Authorization header came),
never their values.
"""
import json
import os
import time

import uvicorn
from fastapi import FastAPI, Request

LOG = os.environ.get("T1_TOOL_LOG", "tool.jsonl")
PORT = int(os.environ.get("T1_TOOL_PORT", "18300"))

app = FastAPI(title="T1 tools", version="1.0",
              servers=[{"url": f"http://127.0.0.1:{PORT}"}])


@app.get("/lookup", operation_id="lookup_fact", summary="Look up a fact for the user")
async def lookup(request: Request, q: str = ""):
    """Return a fixed fact, so the stored answer can be checked."""
    with open(LOG, "a") as f:
        f.write(json.dumps({"ts": time.time(), "path": "/lookup", "q": q,
                            "header_names": sorted(request.headers.keys()),
                            "authorization": "authorization" in request.headers}) + "\n")
    return {"fact": "the T1b fact is 4217"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
