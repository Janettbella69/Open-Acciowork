import asyncio
import json
import os
import signal
import sys
import time

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from test_sdk_adapter import build_runtime

from ophira_backend.config import Settings
from ophira_backend.worker_contract import WorkerLaunchPlan, WorkerLimits
from ophira_backend.worker_envelope import SdkWorkerEnvelope, WorkerResourceError
from ophira_backend.worker_launch import scope_command, verify_cgroup


def options():
    return ClaudeAgentOptions(
        env={"ANTHROPIC_API_KEY": "synthetic-selected-provider"},
        sandbox={"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False},
    )


def wait_for_file(path):
    deadline = time.monotonic() + 5
    while not path.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError("child process did not write its expected signal")
        time.sleep(0.02)


async def invoke(tmp_path, code, *, limits=None, input_data=b"", environment=None):
    settings = Settings(data_dir=tmp_path, worker_limits=limits or WorkerLimits())
    envelope = SdkWorkerEnvelope(
        settings,
        options(),
        command=[sys.executable, "-I", "-c", code],
    )
    captured = {}
    try:
        async with envelope as limited:
            process = await asyncio.create_subprocess_exec(
                str(limited.cli_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **limited.env, **(environment or {})},
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(input_data), timeout=12)
            captured.update(stdout=stdout, stderr=stderr, code=process.returncode)
    except WorkerResourceError as error:
        captured["error"] = str(error)
    captured["report"] = json.loads((envelope.directory / "finished.json").read_bytes())
    captured["started"] = (
        json.loads((envelope.directory / "started.json").read_bytes())
        if (envelope.directory / "started.json").is_file()
        else None
    )
    return captured


async def test_supervisor_relays_binary_stdio_and_only_selected_environment(tmp_path):
    result = await invoke(
        tmp_path,
        "import os,sys; data=sys.stdin.buffer.read(); "
        "assert os.environ['ANTHROPIC_API_KEY']=='synthetic-selected-provider'; "
        "assert 'UNRELATED_CONTROL_SECRET' not in os.environ; "
        "assert 'DBUS_SESSION_BUS_ADDRESS' not in os.environ; "
        "sys.stdout.buffer.write(data)",
        input_data="真实 UTF-8 \x00 bytes".encode(),
        environment={
            "UNRELATED_CONTROL_SECRET": "must-not-leak",
            "DBUS_SESSION_BUS_ADDRESS": "private-bus",
        },
    )
    assert result["code"] == 0 and result["stdout"] == "真实 UTF-8 \x00 bytes".encode()
    assert result["stderr"] == b"" and "error" not in result
    assert result["report"]["output_bytes"] == len(result["stdout"])
    assert result["started"]["kernel_limits_verified"] is False


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_flood_is_terminated_before_excess_bytes_reach_the_sdk(tmp_path, stream):
    result = await invoke(
        tmp_path,
        f"import sys,time; sys.{stream}.write('synthetic-private-output'*10000); "
        f"sys.{stream}.flush(); time.sleep(20)",
        limits=WorkerLimits(output_bytes=1024),
    )
    assert "output limit" in result["error"]
    assert result["report"]["outcome"] == "output_limit"
    assert len(result["stdout"]) <= 1024
    assert result["stderr"] == b""


async def test_wall_clock_terminates_a_silent_worker_and_ordinary_descendant(tmp_path):
    sentinel = tmp_path / "heartbeat"
    child = (
        "import pathlib,time; p=pathlib.Path(" + repr(str(sentinel)) + "); "
        "\nwhile True: p.write_text(str(time.time())); time.sleep(.02)"
    )
    result = await invoke(
        tmp_path,
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(20)",
        limits=WorkerLimits(timeout_seconds=1),
    )
    assert result["report"]["outcome"] == "wall_clock_limit"
    assert "original deadline" in result["error"]
    before = sentinel.read_text()
    await asyncio.sleep(0.1)
    assert sentinel.read_text() == before


async def test_completed_cli_cannot_leave_background_processes_running(tmp_path):
    sentinel = tmp_path / "heartbeat"
    child = (
        "import pathlib,time; p=pathlib.Path(" + repr(str(sentinel)) + "); "
        "\nwhile True: p.write_text(str(time.time())); time.sleep(.02)"
    )
    result = await invoke(
        tmp_path,
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(.1)",
    )
    assert result["report"]["outcome"] == "descendants_remaining"
    before = sentinel.read_text()
    await asyncio.sleep(0.1)
    assert sentinel.read_text() == before


async def test_supervisor_stops_its_worker_after_the_host_process_dies(tmp_path):
    sentinel = tmp_path / "orphan-heartbeat"
    code = (
        f"import pathlib,time; p=pathlib.Path({str(sentinel)!r}); "
        "\nwhile True: p.write_text(str(time.time())); time.sleep(.02)"
    )
    envelope = SdkWorkerEnvelope(
        Settings(data_dir=tmp_path),
        options(),
        command=[sys.executable, "-I", "-c", code],
    )
    supervisor_pid = None
    parent = None
    with pytest.raises(WorkerResourceError, match="termination could not be verified"):
        async with envelope as limited:
            try:
                parent = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-c",
                    "import subprocess,time; "
                    f"p=subprocess.Popen([{str(limited.cli_path)!r}],"
                    "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
                    "stderr=subprocess.DEVNULL); print(p.pid,flush=True); time.sleep(20)",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                supervisor_pid = int(await asyncio.wait_for(parent.stdout.readline(), 5))
                await asyncio.to_thread(wait_for_file, sentinel)
                parent.kill()
                await parent.wait()
                await asyncio.to_thread(wait_for_file, envelope.directory / "finished.json")
                report = json.loads((envelope.directory / "finished.json").read_bytes())
                assert report["outcome"] == "parent_exited"
                before = sentinel.read_text()
                await asyncio.sleep(0.1)
                assert sentinel.read_text() == before
            finally:
                if parent is not None and parent.returncode is None:
                    parent.kill()
                    await parent.wait()
                if supervisor_pid is not None:
                    try:
                        os.kill(supervisor_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass


@pytest.mark.parametrize(
    "report",
    [
        None,
        {"outcome": "unconfirmed", "output_bytes": 0},
        {"outcome": "completed", "output_bytes": True},
        {"outcome": "completed", "output_bytes": 2**60},
        {"outcome": "completed", "output_bytes": 1, "exit_code": "not-a-number"},
    ],
)
async def test_lost_report_blocks_resume_without_fabricating_output_usage(tmp_path, report):
    ledger, run, runtime = build_runtime(tmp_path)
    envelope = SdkWorkerEnvelope(
        runtime.settings,
        options(),
        ledger=ledger,
        run_id=run.id,
        command=[sys.executable, "-c", "pass"],
    )
    with pytest.raises(WorkerResourceError, match="termination could not be verified"):
        async with envelope:
            if report is not None:
                (envelope.directory / "finished.json").write_text(json.dumps(report))
    current = ledger.get_run(run.id)
    assert current.worker_output_bytes == 0
    assert current.worker_output_exhausted is True
    assert current.worker_last_outcome == "unconfirmed"
    with pytest.raises(WorkerResourceError, match="previous SDK output total is unconfirmed"):
        async with SdkWorkerEnvelope(runtime.settings, options(), ledger=ledger, run_id=run.id):
            pytest.fail("unknown usage must not renew the output allowance")


async def test_host_crash_before_context_exit_leaves_a_durable_unaccounted_segment(tmp_path):
    ledger, run, runtime = build_runtime(tmp_path)
    interrupted = SdkWorkerEnvelope(
        runtime.settings,
        options(),
        ledger=ledger,
        run_id=run.id,
        command=[sys.executable, "-c", "pass"],
    )
    await interrupted.__aenter__()
    # Simulate loss of the entire host: no __aexit__ and no in-memory cleanup.
    assert ledger.get_run(run.id).worker_segment_id == interrupted.directory.name
    with pytest.raises(WorkerResourceError, match="previous SDK output total is unconfirmed"):
        async with SdkWorkerEnvelope(runtime.settings, options(), ledger=ledger, run_id=run.id):
            pytest.fail("a durable in-flight claim must survive loss of the host context")
    current = ledger.get_run(run.id)
    assert current.worker_output_bytes == 0 and current.worker_output_exhausted
    assert current.worker_last_outcome == "unconfirmed"
    assert not current.worker_kernel_limits_verified


async def test_frozen_run_limits_and_output_accounting_survive_native_sdk_segments(tmp_path):
    ledger, run, runtime = build_runtime(tmp_path)
    runtime.settings.worker_limits = WorkerLimits(output_bytes=1024, timeout_seconds=20)
    first_deadline = None
    for segment in range(2):
        envelope = SdkWorkerEnvelope(
            runtime.settings,
            options(),
            ledger=ledger,
            run_id=run.id,
            command=[sys.executable, "-I", "-c", "print('x'*599)"],
        )
        if segment == 1:
            runtime.settings.worker_limits = WorkerLimits(output_bytes=10000, timeout_seconds=400)
        try:
            async with envelope as limited:
                assert limited.max_buffer_size == 4 * 1024**2
                assert limited.sandbox["enabled"] and limited.sandbox["failIfUnavailable"]
                assert (
                    str(tmp_path / "runtime-workers") in limited.sandbox["filesystem"]["denyWrite"]
                )
                current = ledger.get_run(run.id)
                if first_deadline is None:
                    first_deadline = current.sdk_wall_clock_deadline
                assert current.sdk_wall_clock_deadline == first_deadline
                assert current.worker_limits.output_bytes == 1024
                process = await asyncio.create_subprocess_exec(
                    str(limited.cli_path),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(process.communicate(), timeout=8)
        except WorkerResourceError:
            assert segment == 1
    current = ledger.get_run(run.id)
    assert current.worker_output_bytes == 1200
    assert current.worker_last_outcome == "output_limit"
    assert current.worker_segment_id is None
    with pytest.raises(WorkerResourceError, match="output limit"):
        async with SdkWorkerEnvelope(runtime.settings, options(), ledger=ledger, run_id=run.id):
            pytest.fail("exhausted Run must not launch again")


def plan(tmp_path, **changes):
    return WorkerLaunchPlan(
        command=[sys.executable],
        unit="ophira-sdk-" + "a" * 32 + ".scope",
        limits=WorkerLimits(mode="systemd", **changes),
        output_remaining=1024,
        deadline_epoch=time.time() + 5.0,
        environment_names=[],
    )


@pytest.mark.parametrize(
    "missing",
    [None, "cpu.max", "memory.max", "memory.swap.max", "pids.max", "memory.oom.group"],
)
def test_kernel_gate_reads_actual_cgroup_limits_and_fails_closed(tmp_path, missing):
    current = plan(tmp_path)
    group = tmp_path / "cgroup" / "user.slice" / current.unit
    group.mkdir(parents=True)
    proc = tmp_path / "proc"
    proc.write_text(f"0::/user.slice/{current.unit}\n")
    for name, value in {
        "cpu.max": "200000 100000",
        "memory.max": str(2 * 1024**3),
        "memory.swap.max": "0",
        "pids.max": "128",
        "memory.oom.group": "1",
    }.items():
        (group / name).write_text("max" if name == missing else value)
    if missing:
        with pytest.raises(ValueError):
            verify_cgroup(current, proc=proc, root=tmp_path / "cgroup")
    else:
        verify_cgroup(current, proc=proc, root=tmp_path / "cgroup")


def test_systemd_command_cannot_expand_sdk_arguments_or_disclose_them_as_unit_description(tmp_path):
    current = plan(tmp_path)
    command = ["binary", "literal ${ANTHROPIC_API_KEY}", "$(do-not-execute)"]
    wrapped = scope_command(current, command)
    assert "--expand-environment=no" in wrapped
    assert "--description=OPHIRA SDK worker" in wrapped
    assert wrapped[-3:] == command
    assert "--property=MemorySwapMax=0" in wrapped
    assert "--property=TasksMax=128" in wrapped
    assert "--property=CPUQuota=200%" in wrapped


async def test_required_kernel_enforcement_has_no_native_fallback_on_mac(tmp_path, monkeypatch):
    monkeypatch.setattr("ophira_backend.worker_envelope.sys.platform", "darwin")
    settings = Settings(data_dir=tmp_path, worker_limits=WorkerLimits(mode="systemd"))
    with pytest.raises(WorkerResourceError, match="cgroup v2"):
        async with SdkWorkerEnvelope(settings, options()):
            pytest.fail("must not launch without required enforcement")


async def test_real_installed_sdk_uses_public_cli_wrapper_and_preserves_wire_frames(tmp_path):
    # A deterministic CLI fixture, not a model or an alternative product runtime.
    fixture = tmp_path / "synthetic_cli.py"
    fixture.write_text("""
import json,sys
if sys.argv[1:] == ['-v']:
    print('2.1.238'); raise SystemExit()
for line in sys.stdin:
    value=json.loads(line)
    if value['type']=='control_request':
        print(json.dumps({'type':'control_response','response':{
            'subtype':'success','request_id':value['request_id'],'response':{}}}), flush=True)
    elif value['type']=='user':
        print(json.dumps({'type':'result','subtype':'success','is_error':False,
            'duration_ms':1,'duration_api_ms':1,'num_turns':1,'session_id':'synthetic-session',
            'result':'verbatim 中文 \\n response'}), flush=True)
""")
    envelope = SdkWorkerEnvelope(
        Settings(data_dir=tmp_path),
        options(),
        command=[sys.executable, "-I", str(fixture)],
    )
    async with envelope as limited, ClaudeSDKClient(options=limited) as client:
        await client.query("synthetic prompt with \n and ${literal}")
        messages = [message async for message in client.receive_response()]
    assert isinstance(messages[-1], ResultMessage)
    assert messages[-1].result == "verbatim 中文 \n response"
    report = json.loads((envelope.directory / "finished.json").read_bytes())
    assert report["outcome"] in {"completed", "terminated"}
    assert report["output_bytes"] > 0


def test_settings_accept_one_validated_json_resource_envelope(monkeypatch, tmp_path):
    monkeypatch.setenv("OPHIRA_WORKER_LIMITS", '{"mode":"systemd","tasks_max":64}')
    settings = Settings(data_dir=tmp_path)
    assert settings.worker_limits.mode == "systemd" and settings.worker_limits.tasks_max == 64
    monkeypatch.setenv("OPHIRA_WORKER_LIMITS", '{"mode":"systemd","tasks_max":true}')
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path)
