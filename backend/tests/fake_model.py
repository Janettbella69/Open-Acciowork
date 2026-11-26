"""A scripted stand-in for an Anthropic-compatible model endpoint.

The real Claude Code CLI talks to it exactly as it would talk to DeepSeek or Bailian,
so the whole loop (tools, hooks, permissions, sandbox, sessions, memory) runs for
real without spending anything. It records every request for the test to inspect.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

QUESTION = "目标市场先选哪个？"
OUTSIDE = Path("/tmp/outside-the-project.txt")


def system_text(body: dict[str, Any]) -> str:
    system = body.get("system") or ""
    if isinstance(system, list):
        return "\n".join(block.get("text", "") for block in system if isinstance(block, dict))
    return str(system)


def all_text(body: dict[str, Any]) -> str:
    return system_text(body) + "\n" + json.dumps(body.get("messages", []), ensure_ascii=False)


def tool_results(body: dict[str, Any]) -> list[dict[str, Any]]:
    found = []
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            found += [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    return found


def user_texts(body: dict[str, Any]) -> list[str]:
    texts = []
    for message in body.get("messages", []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts += [b.get("text", "") for b in content
                      if isinstance(b, dict) and b.get("type") == "text"]
    return texts


def find_path(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    if not match:
        raise AssertionError(f"pattern {pattern!r} not found in the request")
    return match.group(1)


class FakeModel:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def main_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests
                if any(t.get("name") == "Write" for t in r.get("tools", []))]

    def reply(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """Content blocks for the next assistant turn."""
        if not any(t.get("name") == "Write" for t in body.get("tools", [])):
            return [{"type": "text", "text": "ok"}]  # titles, summaries, warmups
        texts = user_texts(body)
        if any("第二轮" in t for t in texts):
            # The CLI hands the memory index to the model as a context reminder in the
            # first user message, rebuilt from the current MEMORY.md on every start.
            remembered = any("auto-memory" in t and "宠物用品到美国" in t for t in texts)
            return [{"type": "text", "text": f"记忆{'在' if remembered else '不在'}"}]
        prompt = system_text(body)
        cwd = find_path(r"[Ww]orking directory: (\S+)", prompt)
        memory_dir = find_path(r"(/\S+/memory)/", prompt)
        step = len(tool_results(body))
        plan = [
            ("Write", {"file_path": f"{cwd}/plan.md", "content": "# 选品计划\n"}),
            ("Bash", {"command": "ls", "description": "看看工作区"}),
            ("Bash", {"command": "python -c \"import openpyxl, docx, pptx; print('office ok')\"",
                      "description": "确认能写办公文件"}),
            ("Write", {"file_path": f"{memory_dir}/MEMORY.md",
                       "content": "- 用户想卖宠物用品到美国\n"}),
            ("Write", {"file_path": str(OUTSIDE), "content": "x"}),
            ("Bash", {"command": "rm plan.md", "description": "删掉草稿"}),
            ("AskUserQuestion", {"questions": [{
                "question": QUESTION, "header": "市场", "multiSelect": False,
                "options": [{"label": "美国", "description": "市场大"},
                            {"label": "欧洲", "description": "合规多"}]}]}),
        ]
        if step < len(plan):
            name, tool_input = plan[step]
            return [{"type": "tool_use", "id": f"toolu_{step:02d}", "name": name,
                     "input": tool_input}]
        return [{"type": "text", "text": "第一轮完成"}]


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def stream(blocks: list[dict[str, Any]], model: str) -> list[bytes]:
    stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    out = [sse("message_start", {"type": "message_start", "message": {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1}}})]
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            out.append(sse("content_block_start", {"type": "content_block_start", "index": index,
                                                   "content_block": {"type": "text", "text": ""}}))
            out.append(sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                                   "delta": {"type": "text_delta",
                                                             "text": block["text"]}}))
        else:
            out.append(sse("content_block_start", {"type": "content_block_start", "index": index,
                                                   "content_block": {**block, "input": {}}}))
            out.append(sse("content_block_delta", {"type": "content_block_delta", "index": index,
                                                   "delta": {"type": "input_json_delta",
                                                             "partial_json": json.dumps(
                                                                 block["input"],
                                                                 ensure_ascii=False)}}))
        out.append(sse("content_block_stop", {"type": "content_block_stop", "index": index}))
    out.append(sse("message_delta", {"type": "message_delta",
                                     "delta": {"stop_reason": stop, "stop_sequence": None},
                                     "usage": {"output_tokens": 5}}))
    out.append(sse("message_stop", {"type": "message_stop"}))
    return out


def create_fake_model_app(model: FakeModel) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/messages/count_tokens")
    async def count_tokens() -> dict[str, int]:
        return {"input_tokens": 100}

    @app.post("/v1/messages")
    async def messages(request: Request):
        body = await request.json()
        model.requests.append(body | {"_auth": request.headers.get("authorization", "")})
        blocks = model.reply(body)
        if body.get("stream"):
            chunks = stream(blocks, body.get("model", "fake-model"))

            async def body_iter():
                for chunk in chunks:
                    yield chunk

            return StreamingResponse(body_iter(), media_type="text/event-stream")
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return JSONResponse({"id": "msg_fake", "type": "message", "role": "assistant",
                             "model": body.get("model"), "content": blocks,
                             "stop_reason": stop, "stop_sequence": None,
                             "usage": {"input_tokens": 10, "output_tokens": 5}})

    return app


# -- running it: shared by the end-to-end test and deploy/shared-box/sandbox_acceptance.py


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("server did not start")


def drive(http: httpx.Client, project_id: str, prompt: str, decide) -> list[dict]:
    run = http.post(f"/api/projects/{project_id}/runs", json={"prompt": prompt}).json()
    events = []
    with http.stream("GET", f"/api/runs/{run['id']}/events", timeout=180) as response:
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            event = json.loads(line[5:])
            events.append(event)
            if event["type"] == "permission.requested":
                http.post(f"/api/runs/{run['id']}/permissions/{event['data']['request_id']}",
                          json=decide(event["data"]))
            if event["type"] == "run.finished":
                break
    return events


def person(request: dict) -> dict:
    if request["tool_name"] == "AskUserQuestion":
        return {"allow": True, "answers": {QUESTION: "美国"}}
    if request["tool_name"] == "Bash" and "rm" in request["tool_input"].get("command", ""):
        return {"allow": False, "message": "先别删"}
    return {"allow": False, "message": f"unexpected request: {request['tool_name']}"}
