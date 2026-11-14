"""A real embedding model behind OpenAI's embeddings API, for MemMachine core in the real e2e run.

sentence-transformers/all-MiniLM-L6-v2 (384 dimensions), its ONNX export from the local Hugging
Face cache, on CPU: real semantic vectors without an external provider or key. Core's openai
embedder sends `dimensions` on every call and fails an answer of another length, so a call asking
for anything but 384 is refused.

    E2E_EMBED_LOG=$T1_ENV/logs/embed.jsonl python e2e/embed_server.py   # 127.0.0.1:18210
"""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

MODEL = "all-MiniLM-L6-v2"
DIM = 384
SNAP = Path.home() / ".cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/snapshots"
snap = next(p for p in SNAP.iterdir() if (p / "onnx" / "model.onnx").exists())
tok = Tokenizer.from_file(str(snap / "tokenizer.json"))
tok.enable_truncation(max_length=256)
tok.enable_padding(pad_id=0, pad_token="[PAD]")
sess = ort.InferenceSession(str(snap / "onnx" / "model.onnx"), providers=["CPUExecutionProvider"])
names = {i.name for i in sess.get_inputs()}
LOG = os.environ.get("E2E_EMBED_LOG")


def embed(texts):
    enc = tok.encode_batch(texts)
    ids = np.array([e.ids for e in enc], dtype=np.int64)
    mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
    feed = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in names:
        feed["token_type_ids"] = np.zeros_like(ids)
    out = sess.run(None, feed)[0]                       # (batch, tokens, 384)
    m = mask[..., None].astype(np.float32)
    v = (out * m).sum(1) / np.clip(m.sum(1), 1e-9, None)  # mean pooling
    return v / np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-12, None)


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        self._send(200 if self.path == "/health" else 404, {"ok": self.path == "/health"})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/embeddings"):
            return self._send(404, {"error": {"message": "not found"}})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        inp = body.get("input")
        texts = [inp] if isinstance(inp, str) else list(inp or [])
        if body.get("dimensions") not in (None, DIM):
            return self._send(400, {"error": {"message": f"{MODEL} has {DIM} dimensions"}})
        t = time.time()
        vecs = embed(texts) if texts else np.zeros((0, DIM))
        if LOG:
            with open(LOG, "a") as f:
                f.write(json.dumps({"ts": time.time(), "n": len(texts), "ms": round((time.time() - t) * 1000),
                                    "chars": sum(len(x) for x in texts)}) + "\n")
        self._send(200, {"object": "list", "model": MODEL,
                         "data": [{"object": "embedding", "index": i, "embedding": v.tolist()}
                                  for i, v in enumerate(vecs)],
                         "usage": {"prompt_tokens": 0, "total_tokens": 0}})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 18210), H).serve_forever()
