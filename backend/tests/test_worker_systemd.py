"""Actual cgroup v2 tests: mandatory in the Linux resource CI job, no model calls."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from test_worker_envelope import invoke, options

from ophira_backend.config import Settings
from ophira_backend.worker_contract import WorkerLimits
from ophira_backend.worker_envelope import SdkWorkerEnvelope

pytestmark = pytest.mark.skipif(
    os.environ.get("OPHIRA_REQUIRE_SYSTEMD_TESTS") != "1",
    reason="requires the explicit Linux systemd resource acceptance environment",
)


def test_required_acceptance_environment_is_present():
    assert sys.platform == "linux"
    assert Path("/usr/bin/systemd-run").is_file()
    assert Path("/sys/fs/cgroup/cgroup.controllers").is_file()


async def test_unavailable_user_manager_never_falls_back_to_unrestricted_execution(tmp_path):
    marker = tmp_path / "must-not-execute"
    result = await invoke(
        tmp_path,
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
        limits=WorkerLimits(mode="systemd"),
        # systemd first tries XDG_RUNTIME_DIR/systemd/private, then the user bus.
        # Both must be unreachable to simulate an absent manager (v255 bus-util.c).
        environment={
            "XDG_RUNTIME_DIR": str(tmp_path / "missing-user-runtime"),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/ophira-test-bus",
        },
    )
    assert result["code"] != 0 and result["started"] is None
    assert "error" in result and not marker.exists()


async def test_pinned_native_cli_starts_in_the_required_scope_without_a_model_call(tmp_path):
    envelope = SdkWorkerEnvelope(
        Settings(data_dir=tmp_path, worker_limits=WorkerLimits(mode="systemd")),
        options(),
    )
    async with envelope as limited:
        process = await asyncio.create_subprocess_exec(
            str(limited.cli_path),
            "--help",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **limited.env},
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        assert process.returncode == 0 and b"Usage:" in stdout
        assert stderr == b""
        envelope.record_started()
    report = json.loads((envelope.directory / "finished.json").read_bytes())
    assert report["outcome"] == "completed" and report["output_bytes"] == len(stdout)


async def test_actual_cpu_controller_throttles_the_worker_and_reports_verified_limits(tmp_path):
    result = await invoke(
        tmp_path,
        """
import pathlib,time,json
group=pathlib.Path('/sys/fs/cgroup') / pathlib.Path(
    pathlib.Path('/proc/self/cgroup').read_text().strip().split('::',1)[1].lstrip('/'))
until=time.monotonic()+1.1
while time.monotonic()<until:
    pass
stats=dict(line.split() for line in (group/'cpu.stat').read_text().splitlines())
print(json.dumps({'cpu':(group/'cpu.max').read_text(),
    'throttled':int(stats['nr_throttled'])}))
""",
        limits=WorkerLimits(mode="systemd", cpu_percent=25),
    )
    assert "error" not in result, result
    value = json.loads(result["stdout"])
    assert value["cpu"].split() == ["25000", "100000"]
    assert value["throttled"] > 0
    assert result["started"]["kernel_limits_verified"] is True


async def test_memory_exhaustion_is_confined_to_the_kernel_scope(tmp_path):
    result = await invoke(
        tmp_path,
        "payload=bytearray(128*1024*1024); print('must-not-fit')",
        limits=WorkerLimits(mode="systemd", memory_bytes=64 * 1024**2),
    )
    assert result["started"]["kernel_limits_verified"] is True
    assert result["code"] != 0 and result["stdout"] == b""
    assert result["report"]["outcome"] == "worker_failed"


async def test_pid_controller_rejects_excess_children_without_affecting_the_host(tmp_path):
    result = await invoke(
        tmp_path,
        """
import subprocess,sys,errno
children=[]
try:
    for _ in range(24):
        children.append(subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
except OSError as error:
    assert error.errno == errno.EAGAIN
    print('tasks-limited',flush=True)
finally:
    for child in children:
        child.kill()
    for child in children:
        child.wait()
""",
        limits=WorkerLimits(mode="systemd", tasks_max=16),
    )
    assert "error" not in result, result
    assert result["stdout"] == b"tasks-limited\n"
    assert result["started"]["kernel_limits_verified"] is True


async def test_scope_cleanup_kills_a_descendant_that_created_another_process_session(tmp_path):
    sentinel = tmp_path / "detached-heartbeat"
    child = (
        f"import pathlib,time; p=pathlib.Path({str(sentinel)!r}); "
        "\nwhile True: p.write_text(str(time.time())); time.sleep(.02)"
    )
    result = await invoke(
        tmp_path,
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
        "time.sleep(.3)",
        limits=WorkerLimits(mode="systemd"),
    )
    assert result["started"]["kernel_limits_verified"] is True
    assert result["report"]["outcome"] == "descendants_remaining"
    before = sentinel.read_text()
    await asyncio.sleep(0.15)
    assert sentinel.read_text() == before
