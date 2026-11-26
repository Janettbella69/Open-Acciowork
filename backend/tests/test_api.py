"""The HTTP surface around a turn: files, uploads, feedback, origins, replay."""

from __future__ import annotations

import io
import json

from pypdf import PdfWriter


async def test_projects_are_folders(harness):
    project = await harness.project("宠物用品")
    assert project["name"] == "宠物用品"
    listed = (await harness.http.get("/api/projects")).json()
    assert [p["id"] for p in listed] == [project["id"]]
    folder = harness.store.project_dir(project["id"])
    assert {p.name for p in folder.iterdir()} >= {"project.json", "workspace", "claude", "runs",
                                                 "snapshots.git"}
    assert (await harness.http.get("/api/projects/prj_0000000000000000")).status_code == 404
    assert (await harness.http.get("/api/projects/../../etc")).status_code == 404


async def test_files_round_trip_and_stay_inside_the_workspace(harness):
    project = await harness.project()
    pid = project["id"]
    (harness.store.workspace(pid) / "brief.md").write_text("结论", encoding="utf-8")
    files = (await harness.http.get(f"/api/projects/{pid}/files")).json()
    assert [f["path"] for f in files] == ["brief.md"]
    got = await harness.http.get(f"/api/projects/{pid}/files/brief.md")
    assert got.status_code == 200 and got.text == "结论"
    escape = await harness.http.get(f"/api/projects/{pid}/files/..%2Fproject.json")
    assert escape.status_code == 404


async def test_a_pdf_upload_gets_a_markdown_copy_before_the_agent_sees_it(harness):
    project = await harness.project()
    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buffer)
    response = await harness.http.post(
        f"/api/projects/{project['id']}/files",
        files={"file": ("../报价单.pdf", buffer.getvalue(), "application/pdf")})
    assert response.status_code == 201
    saved = response.json()["saved"]
    assert saved == ["uploads/报价单.pdf", "uploads/报价单.pdf.md"]
    companion = harness.store.workspace(project["id"]) / saved[1]
    assert "扫描件" in companion.read_text(encoding="utf-8")


async def test_feedback_is_kept_as_a_file(harness):
    project = await harness.project()
    response = await harness.http.post(f"/api/projects/{project['id']}/feedback",
                                       json={"text": "运费算错了", "run_id": "run_1"})
    assert response.status_code == 201
    lines = (harness.store.project_dir(project["id"]) / "feedback.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert json.loads(lines[0])["text"] == "运费算错了"


async def test_other_sites_cannot_drive_the_api(harness):
    blocked = await harness.http.post("/api/projects", json={"name": "x"},
                                      headers={"origin": "https://evil.example"})
    assert blocked.status_code == 403
    site = await harness.http.post("/api/projects", json={"name": "x"},
                                   headers={"origin": "https://trademind.store"})
    assert site.status_code == 201
    same = await harness.http.post("/api/projects", json={"name": "x"},
                                   headers={"origin": "http://127.0.0.1:8000"})
    assert same.status_code == 201


async def test_finished_turns_replay_from_the_log_after_the_last_seen_event(harness):
    project = await harness.project()
    run = await harness.run_to_end(project["id"])
    async with harness.http.stream("GET", f"/api/runs/{run['id']}/events",
                                   headers={"last-event-id": "2"}) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])
    seqs = [json.loads(line[5:])["seq"] for line in body.splitlines() if line.startswith("data:")]
    assert seqs == [3, 4]


async def test_answering_a_finished_request_is_a_404(harness):
    project = await harness.project()
    run = await harness.run_to_end(project["id"])
    response = await harness.http.post(f"/api/runs/{run['id']}/permissions/req_x",
                                       json={"allow": True})
    assert response.status_code == 404


async def test_a_turn_cut_off_by_a_restart_is_closed_honestly(harness, tmp_path):
    from ophira_backend.main import create_app

    project = await harness.project()
    run = harness.store.create_run(project["id"], "调研")  # never finished: the process died
    harness.store.append_event(project["id"], run["id"], json.dumps(
        {"seq": 1, "at": "t", "source": "host", "type": "run.started", "data": {}}))
    restarted = create_app(harness.settings)
    store = restarted.state.store
    assert store.find_run(run["id"])["status"] == "failed"
    last = store.read_events(project["id"], run["id"])[-1]
    assert last["seq"] == 2 and last["type"] == "run.finished"
    assert last["data"] == {"status": "failed", "error": "后台重启了，这一轮中断了。"}


async def test_health(harness):
    assert (await harness.http.get("/health")).json() == {"ok": True, "model": "fake-model"}
