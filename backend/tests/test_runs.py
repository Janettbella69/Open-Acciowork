"""A turn, end to end through the HTTP API, with the model side scripted."""

from __future__ import annotations

import asyncio
import subprocess

from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolUseBlock,
)
from conftest import result, say


async def wait_for_request(harness, run_id: str) -> dict:
    for _ in range(200):
        live = harness.runner.live.get(run_id)
        if live and live.pending:
            return next(iter(live.pending.values()))[1]
        await asyncio.sleep(0.01)
    raise AssertionError("no permission request appeared")


async def test_a_turn_records_native_sdk_messages_in_order(harness):
    project = await harness.project()
    run = await harness.run_to_end(project["id"], "我想卖宠物用品到美国")
    events = harness.store.read_events(project["id"], run["id"])
    assert [(e["source"], e["type"]) for e in events] == [
        ("host", "run.started"),
        ("sdk", "AssistantMessage"),
        ("sdk", "ResultMessage"),
        ("host", "run.finished"),
    ]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert events[1]["data"]["content"] == [{"type": "TextBlock", "text": "好的"}]
    assert events[-1]["data"] == {"status": "completed", "error": None}
    assert harness.clients[0].prompts == ["我想卖宠物用品到美国"]
    assert harness.store.list_runs(project["id"])[0]["status"] == "completed"


async def test_the_next_turn_resumes_the_sdk_session(harness):
    project = await harness.project()
    await harness.run_to_end(project["id"])
    assert harness.store.get_project(project["id"])["session_id"] == "sess-1"
    await harness.run_to_end(project["id"], "接着说")
    assert harness.clients[0].options.resume is None
    assert harness.clients[1].options.resume == "sess-1"


async def test_one_turn_at_a_time_per_project(harness):
    release = asyncio.Event()

    async def slow(client):
        await release.wait()
        yield result()

    harness.script = slow
    project = await harness.project()
    first = await harness.http.post(f"/api/projects/{project['id']}/runs", json={"prompt": "a"})
    second = await harness.http.post(f"/api/projects/{project['id']}/runs", json={"prompt": "b"})
    assert first.status_code == 201 and second.status_code == 409
    detail = (await harness.http.get(f"/api/projects/{project['id']}")).json()
    assert detail["active_run_id"] == first.json()["id"]
    release.set()
    await harness.runner.wait_idle()


async def test_a_risky_action_waits_for_the_person_and_obeys_them(harness):
    seen = {}

    async def wants_to_run(client):
        verdict = await client.options.can_use_tool(
            "Bash", {"command": "rm -rf drafts"}, type("Ctx", (), {"tool_use_id": "tu-7"})()
        )
        seen["verdict"] = verdict
        yield result()

    harness.script = wants_to_run
    project = await harness.project()
    run = (await harness.http.post(f"/api/projects/{project['id']}/runs",
                                   json={"prompt": "清理草稿"})).json()
    request = await wait_for_request(harness, run["id"])
    assert request["tool_name"] == "Bash" and request["tool_use_id"] == "tu-7"
    response = await harness.http.post(
        f"/api/runs/{run['id']}/permissions/{request['request_id']}",
        json={"allow": False, "message": "先别删"})
    assert response.status_code == 200
    await harness.runner.wait_idle()
    assert isinstance(seen["verdict"], PermissionResultDeny)
    assert seen["verdict"].message == "先别删"
    types = [e["type"] for e in harness.store.read_events(project["id"], run["id"])]
    assert "permission.requested" in types and "permission.resolved" in types


async def test_clarifying_questions_return_the_persons_answers(harness):
    questions = [{"question": "目标市场选哪个？", "header": "市场", "multiSelect": False,
                  "options": [{"label": "美国", "description": ""},
                              {"label": "欧洲", "description": ""}]}]
    seen = {}

    async def asks(client):
        seen["verdict"] = await client.options.can_use_tool(
            "AskUserQuestion", {"questions": questions}, None)
        yield result()

    harness.script = asks
    project = await harness.project()
    run = (await harness.http.post(f"/api/projects/{project['id']}/runs",
                                   json={"prompt": "帮我选市场"})).json()
    request = await wait_for_request(harness, run["id"])
    await harness.http.post(f"/api/runs/{run['id']}/permissions/{request['request_id']}",
                            json={"allow": True, "answers": {"目标市场选哪个？": "美国"}})
    await harness.runner.wait_idle()
    assert isinstance(seen["verdict"], PermissionResultAllow)
    assert seen["verdict"].updated_input == {"questions": questions,
                                             "answers": {"目标市场选哪个？": "美国"}}


async def test_edits_inside_the_project_need_no_click(harness):
    seen = {}

    async def edits(client):
        seen["verdict"] = await client.options.can_use_tool(
            "Write", {"file_path": "plan.md", "content": "x"}, None)
        yield result()

    harness.script = edits
    project = await harness.project()
    await harness.run_to_end(project["id"])
    assert isinstance(seen["verdict"], PermissionResultAllow)


async def test_the_hook_refuses_outside_writes_and_sends_deletes_to_the_person(harness):
    seen = {}

    async def probes(client):
        seen["outside"] = await client.pre_tool_use("Write", {"file_path": "/etc/hosts"})
        seen["inside"] = await client.pre_tool_use("Write", {"file_path": "notes.md"})
        seen["delete"] = await client.pre_tool_use("Bash", {"command": "rm old.csv"})
        seen["listing"] = await client.pre_tool_use("Bash", {"command": "ls -la"})
        yield result()

    harness.script = probes
    project = await harness.project()
    await harness.run_to_end(project["id"])
    assert seen["outside"]["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert seen["inside"] == {} and seen["listing"] == {}
    assert seen["delete"]["hookSpecificOutput"]["permissionDecision"] == "ask"


async def test_stopping_a_turn(harness):
    async def long_turn(client):
        yield say("开始调研")
        await client.interrupted.wait()
        yield result(is_error=True, text="interrupted")

    harness.script = long_turn
    project = await harness.project()
    run = (await harness.http.post(f"/api/projects/{project['id']}/runs",
                                   json={"prompt": "调研"})).json()
    for _ in range(100):
        if harness.clients and harness.runner.live.get(run["id"]).client:
            break
        await asyncio.sleep(0.01)
    assert (await harness.http.post(f"/api/runs/{run['id']}/cancel")).json() == {"cancelled": True}
    await harness.runner.wait_idle()
    assert harness.store.find_run(run["id"])["status"] == "cancelled"


async def test_a_crashed_turn_is_recorded_not_hidden(harness):
    async def crashes(client):
        yield say("先查一下")
        raise RuntimeError("CLI exited")

    harness.script = crashes
    project = await harness.project()
    run = await harness.run_to_end(project["id"])
    stored = harness.store.find_run(run["id"])
    assert stored["status"] == "failed" and "CLI exited" in stored["error"]
    assert harness.store.read_events(project["id"], run["id"])[-1]["data"]["status"] == "failed"


async def test_every_turn_is_bracketed_by_workspace_snapshots(harness):
    async def writes_a_file(client):
        (harness.store.workspace(client.options.cwd.split("/")[-2]) / "brief.md").write_text("hi")
        yield AssistantMessage(content=[ToolUseBlock(id="t", name="Write", input={})],
                               model="fake-model")
        yield result()

    harness.script = writes_a_file
    project = await harness.project()
    run = await harness.run_to_end(project["id"])
    git_dir = harness.store.project_dir(project["id"]) / "snapshots.git"
    log = subprocess.run(["git", f"--git-dir={git_dir}", "log", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout.split()
    # Nothing changed between creation and the turn, so the "before" snapshot was a no-op.
    assert log == ["after", run["id"], "project", "created"]
    shown = subprocess.run(["git", f"--git-dir={git_dir}", "show", "HEAD:brief.md"],
                           capture_output=True, text=True, check=True).stdout
    assert shown == "hi"
    assert not (harness.store.workspace(project["id"]) / ".git").exists()
