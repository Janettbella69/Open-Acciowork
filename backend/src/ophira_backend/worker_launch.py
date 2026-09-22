"""Byte-transparent supervisor for the public SDK cli_path extension.

No model protocol or tool dispatch is implemented here. The SDK still starts
and controls its pinned native CLI. All child data is bounded before forwarding.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# -I excludes the current working directory and script directory. Load only the
# adjacent, installed contract; never import code from the Agent workspace.
_spec = importlib.util.spec_from_file_location(
    "ophira_worker_contract",
    Path(__file__).with_name("worker_contract.py"),
)
_contract = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _contract
_spec.loader.exec_module(_contract)
WorkerLaunchPlan = _contract.WorkerLaunchPlan
WorkerReport = _contract.WorkerReport

_CHUNK = _contract.OUTPUT_CHUNK_BYTES
_SAFE_ENV = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
}


def read_plan(path: Path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError("invalid worker plan")
    return WorkerLaunchPlan.model_validate_json(path.read_bytes())


def write_report(directory, name, value):
    temporary = directory / (name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, separators=(",", ":"), allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / name)


def scope_command(plan, command):
    return [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--no-ask-password",
        "--expand-environment=no",
        "--description=OPHIRA SDK worker",
        "--unit=" + plan.unit,
        "--property=CPUQuota=" + str(plan.limits.cpu_percent) + "%",
        "--property=MemoryMax=" + str(plan.limits.memory_bytes),
        "--property=MemorySwapMax=0",
        "--property=TasksMax=" + str(plan.limits.tasks_max),
        "--property=RuntimeMaxSec=" + str(max(1, int(plan.deadline_epoch - time.time()))),
        "--property=KillMode=control-group",
        "--property=OOMPolicy=kill",
        "--",
        *command,
    ]


def verify_cgroup(plan, *, proc=Path("/proc/self/cgroup"), root=Path("/sys/fs/cgroup")):
    lines = proc.read_text().splitlines()
    groups = [line[3:] for line in lines if line.startswith("0::")]
    if len(groups) != 1 or not groups[0].startswith("/"):
        raise ValueError("cgroup v2 is required")
    relative = Path(groups[0].lstrip("/"))
    if ".." in relative.parts or relative.name != plan.unit:
        raise ValueError("worker is outside its assigned scope")
    group = (root / relative).resolve(strict=True)
    group.relative_to(root.resolve(strict=True))
    cpu = (group / "cpu.max").read_text().split()
    if len(cpu) != 2 or not all(value.isascii() and value.isdigit() for value in cpu):
        raise ValueError("CPU quota is unavailable")
    quota, period = map(int, cpu)
    if not 0 < quota * 100 <= plan.limits.cpu_percent * period:
        raise ValueError("CPU quota is not enforced")
    for filename, maximum in (
        ("memory.max", plan.limits.memory_bytes),
        ("pids.max", plan.limits.tasks_max),
    ):
        value = (group / filename).read_text().strip()
        if not value.isascii() or not value.isdigit() or not 0 < int(value) <= maximum:
            raise ValueError("memory or task limit is not enforced")
    if (group / "memory.swap.max").read_text().strip() != "0":
        raise ValueError("swap limit is not enforced")
    if (group / "memory.oom.group").read_text().strip() != "1":
        raise ValueError("whole-scope OOM cleanup is not enforced")


def stop_scope(unit):
    """Fixed generated unit only; no PID lookup, shell or wildcard stopping."""
    try:
        subprocess.run(
            [
                "/usr/bin/systemctl",
                "--user",
                "--no-ask-password",
                "kill",
                "--signal=KILL",
                "--kill-whom=all",
                unit,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "--no-ask-password", "stop", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        state = (
            subprocess.run(
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "--no-ask-password",
                    "show",
                    "--property=LoadState",
                    "--property=ActiveState",
                    unit,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
            .stdout.decode("ascii")
            .splitlines()
        )
        fields = dict(line.split("=", 1) for line in state if "=" in line)
        return fields.get("LoadState") == "not-found" or fields.get("ActiveState") in {
            "inactive",
            "failed",
        }
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return False


def execute(plan, directory, arguments):
    if time.time() >= plan.deadline_epoch:
        return 124
    if plan.limits.mode == "systemd":
        verify_cgroup(plan)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _SAFE_ENV or key in plan.environment_names
    }
    # D-Bus/control-group management is host state, not a tool capability.
    for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "SYSTEMD_EXEC_PID"):
        environment.pop(key, None)
    write_report(
        directory,
        "started.json",
        {
            "mode": plan.limits.mode,
            "kernel_limits_verified": plan.limits.mode == "systemd",
        },
    )
    os.execve(plan.command[0], [*plan.command, *arguments], environment)


def supervise(plan, plan_path, arguments):
    directory = plan_path.parent
    if arguments in (["-v"], ["--version"]):
        # SDK's bounded executable-version probe runs before options.env exists.
        os.execv(plan.command[0], [*plan.command, *arguments])
    command = [
        sys.executable,
        "-I",
        str(Path(__file__).resolve()),
        "exec",
        str(plan_path),
        *arguments,
    ]
    if plan.limits.mode == "systemd":
        command = scope_command(plan, command)
    state = {"outcome": None, "output_bytes": 0}
    deadline = time.monotonic() + max(0, plan.deadline_epoch - time.time())
    lock, stop = threading.Lock(), threading.Event()
    parent = os.getppid()
    process = None

    def reason(value):
        with lock:
            if state["outcome"] is None:
                state["outcome"] = value
        stop.set()

    def relay_output(source, destination):
        try:
            while chunk := os.read(source, _CHUNK):
                with lock:
                    remaining = plan.output_remaining - state["output_bytes"]
                    state["output_bytes"] += len(chunk)
                if len(chunk) > remaining:
                    reason("output_limit")
                    return
                # Raw stderr is counted but withheld: it can contain provider
                # credentials or raw tool data before the host redaction hooks.
                if destination is not None:
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(destination, view) :]
        except (OSError, BrokenPipeError):
            reason("disconnected")

    def relay_input(destination):
        try:
            while chunk := os.read(0, _CHUNK):
                view = memoryview(chunk)
                while view:
                    view = view[os.write(destination, view) :]
            os.close(destination)
        except (OSError, BrokenPipeError):
            pass

    previous_signals = {}
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous_signals[number] = signal.signal(number, lambda *_: reason("terminated"))
    try:
        if time.monotonic() >= deadline:
            reason("wall_clock_limit")
        else:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            input_descriptor = os.dup(process.stdin.fileno())
            process.stdin.close()
            threads = [
                threading.Thread(target=relay_input, args=(input_descriptor,), daemon=True),
                threading.Thread(
                    target=relay_output, args=(process.stdout.fileno(), 1), daemon=True
                ),
                threading.Thread(
                    target=relay_output, args=(process.stderr.fileno(), None), daemon=True
                ),
            ]
            for thread in threads:
                thread.start()
            while process.poll() is None and not stop.wait(0.05):
                if time.monotonic() >= deadline:
                    reason("wall_clock_limit")
                elif os.getppid() != parent:
                    reason("parent_exited")
            # Drain bytes already produced before recording success. Descendants
            # retaining pipes cannot keep a terminal native CLI alive indefinitely.
            if process.poll() is not None and not stop.is_set():
                for thread in threads[1:]:
                    thread.join(timeout=0.25)
                if any(thread.is_alive() for thread in threads[1:]):
                    reason("descendants_remaining")
    except (OSError, ValueError):
        reason("launch_failed")
    finally:
        if plan.limits.mode == "systemd" and not stop_scope(plan.unit):
            reason("cleanup_failed")
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                reason("cleanup_failed")
        for number, handler in previous_signals.items():
            signal.signal(number, handler)
    with lock:
        if state["outcome"] is None:
            state["outcome"] = (
                "completed" if process and process.returncode == 0 else "worker_failed"
            )
        state["exit_code"] = process.returncode if process else None
        report = WorkerReport.model_validate(dict(state))
    write_report(directory, "finished.json", report.model_dump())
    return 0 if state["outcome"] == "completed" else 1


def main():
    try:
        if len(sys.argv) < 3 or sys.argv[1] not in {"run", "exec"}:
            raise ValueError("invalid worker launch")
        path = Path(sys.argv[2])
        plan = read_plan(path)
        if sys.argv[1] == "exec":
            return execute(plan, path.parent, sys.argv[3:])
        return supervise(plan, path, sys.argv[3:])
    except Exception:
        # No argv, environment, child output or exception text leaves this boundary.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
