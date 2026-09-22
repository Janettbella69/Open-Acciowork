"""Run-scoped host lifecycle for the SDK's public executable wrapper."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import sys
import tempfile
import uuid
from datetime import timedelta
from importlib import resources
from pathlib import Path

from .models import utc_now
from .worker_contract import OUTPUT_CHUNK_BYTES, WorkerLaunchPlan, WorkerReport
from .worker_launch import stop_scope


class WorkerResourceError(RuntimeError):
    pass


_MESSAGES = {
    "output_limit": "The SDK worker exceeded this Run's output limit and was stopped.",
    "wall_clock_limit": "The SDK worker exceeded this Run's original deadline and was stopped.",
    "worker_failed": "The SDK worker exited unsuccessfully within its resource envelope.",
    "launch_failed": "The SDK worker could not start with the configured resource limits.",
    "cleanup_failed": "SDK worker cleanup could not be confirmed.",
}


def _report(path):
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            return None
        value = json.loads(path.read_bytes())
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


class SdkWorkerEnvelope:
    def __init__(self, settings, options, *, ledger=None, run_id=None, command=None):
        self.settings = settings
        self.options = options
        self.ledger, self.run_id = ledger, run_id
        self.limits = settings.worker_limits
        self.directory = None
        self.command = command
        self.plan = None

    async def __aenter__(self):
        return self._prepare()

    def _prepare(self):
        deadline = utc_now() + timedelta(seconds=self.limits.timeout_seconds)
        consumed = 0
        if self.ledger is not None:
            with self.ledger._lock_for(self.run_id):
                run = self.ledger.get_run(self.run_id)
                if run.worker_limits is None:
                    deadline = min(deadline, run.sdk_wall_clock_deadline or deadline)
                    run = self.ledger.update_run(
                        run.id,
                        worker_limits=self.limits,
                        worker_limits_initialized_at=utc_now(),
                        sdk_wall_clock_deadline=deadline,
                    )
                self.limits = run.worker_limits
                deadline = run.sdk_wall_clock_deadline
                consumed = run.worker_output_bytes
                if run.worker_output_exhausted or run.worker_segment_id is not None:
                    if run.worker_segment_id is not None:
                        self.ledger.update_run(
                            run.id,
                            worker_output_exhausted=True,
                            worker_last_outcome="unconfirmed",
                            worker_kernel_limits_verified=False,
                        )
                    raise WorkerResourceError(
                        "The previous SDK output total is unconfirmed. Start a new Run.",
                    )
        if deadline is None or deadline <= utc_now():
            raise WorkerResourceError(_MESSAGES["wall_clock_limit"])
        if consumed >= self.limits.output_bytes:
            raise WorkerResourceError(_MESSAGES["output_limit"])
        if self.limits.mode == "systemd" and (
            sys.platform != "linux"
            or not Path("/usr/bin/systemd-run").is_file()
            or not Path("/usr/bin/systemctl").is_file()
        ):
            raise WorkerResourceError(
                "Linux systemd with a user manager and delegated cgroup v2 controllers is required."
            )
        if self.command is None:
            # The distribution and its executable are pinned together. Do not
            # silently select an unrelated global Claude installation.
            binary = Path(str(resources.files("claude_agent_sdk") / "_bundled" / "claude"))
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise WorkerResourceError(
                    "The pinned SDK's bundled Claude executable is unavailable."
                )
            self.command = [str(binary)]
        root = self.settings.data_dir.resolve() / "runtime-workers"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or root.stat().st_mode & 0o077:
            raise WorkerResourceError("The SDK worker control directory must be private.")
        self.directory = Path(tempfile.mkdtemp(prefix="worker-", dir=root))
        self.plan = WorkerLaunchPlan(
            command=self.command,
            unit=f"ophira-sdk-{uuid.uuid4().hex}.scope",
            limits=self.limits,
            output_remaining=self.limits.output_bytes - consumed,
            deadline_epoch=deadline.timestamp(),
            environment_names=sorted(self.options.env),
        )
        plan_path = self.directory / "plan.json"
        with plan_path.open("x") as stream:
            stream.write(self.plan.model_dump_json())
        plan_path.chmod(0o600)
        script = Path(__file__).with_name("worker_launch.py").resolve()
        wrapper = self.directory / "claude"
        wrapper.write_text(
            "#!/bin/sh\nexec "
            + shlex.join(
                [
                    sys.executable,
                    "-I",
                    str(script),
                    "run",
                    str(plan_path),
                ]
            )
            + ' "$@"\n'
        )
        wrapper.chmod(0o700)
        sandbox = dict(self.options.sandbox or {})
        filesystem = dict(sandbox.get("filesystem") or {})
        denied = [str(root)]
        if self.limits.mode == "systemd":
            denied += [
                "/sys/fs/cgroup",
                f"/run/user/{os.getuid()}/bus",
                f"/run/user/{os.getuid()}/systemd",
                "/run/systemd/private",
            ]
        for name in ("denyRead", "denyWrite"):
            filesystem[name] = list(dict.fromkeys([*filesystem.get(name, []), *denied]))
        sandbox["filesystem"] = filesystem
        options = dataclasses.replace(
            self.options,
            cli_path=wrapper,
            max_buffer_size=self.limits.frame_bytes,
            sandbox=sandbox,
        )
        if self.ledger is not None:
            with self.ledger._lock_for(self.run_id):
                current = self.ledger.get_run(self.run_id)
                if (
                    current.worker_segment_id is not None
                    or current.worker_output_exhausted
                    or current.worker_output_bytes != consumed
                ):
                    for name in ("claude", "plan.json"):
                        (self.directory / name).unlink(missing_ok=True)
                    raise WorkerResourceError("Another SDK segment changed this Run's allowance.")
                self.ledger.update_run(
                    current.id,
                    worker_segment_id=self.directory.name,
                    worker_kernel_limits_verified=False,
                    worker_last_outcome=None,
                )
        return options

    def record_started(self):
        started = _report(self.directory / "started.json")
        if (
            not started
            or started.get("mode") != self.limits.mode
            or (self.limits.mode == "systemd" and started.get("kernel_limits_verified") is not True)
        ):
            raise WorkerResourceError("The configured SDK worker startup could not be verified.")
        if self.ledger is not None:
            self.ledger.update_run(
                self.run_id,
                worker_kernel_limits_verified=self.limits.mode == "systemd",
            )

    async def __aexit__(self, exception_type, _exception, _traceback):
        if self.plan is None:
            return
        cleanup_confirmed = True
        if self.limits.mode == "systemd":
            # Also runs if the SDK killed the supervisor before it could finish.
            cleanup_confirmed = await asyncio.to_thread(stop_scope, self.plan.unit)
        try:
            report = WorkerReport.model_validate(_report(self.directory / "finished.json"))
        except ValueError:
            report = None
        started = _report(self.directory / "started.json")
        outcome = "unconfirmed"
        # A lost report must not reset the allowance or fabricate observed bytes.
        observed = 0
        exhausted = True
        if (
            report
            and report.outcome != "unconfirmed"
            and report.output_bytes <= (self.plan.output_remaining + 2 * OUTPUT_CHUNK_BYTES)
        ):
            outcome = report.outcome
            observed = report.output_bytes
            exhausted = False
        if not cleanup_confirmed:
            outcome = "cleanup_failed"
        if self.ledger is not None:
            with self.ledger._lock_for(self.run_id):
                run = self.ledger.get_run(self.run_id)
                if run.worker_segment_id != self.directory.name:
                    observed, exhausted, outcome = 0, True, "unconfirmed"
                self.ledger.update_run(
                    run.id,
                    worker_segment_id=(
                        None
                        if run.worker_segment_id == self.directory.name
                        else run.worker_segment_id
                    ),
                    worker_output_bytes=run.worker_output_bytes + observed,
                    worker_output_exhausted=run.worker_output_exhausted or exhausted,
                    worker_last_outcome=outcome,
                    worker_kernel_limits_verified=bool(
                        started
                        and started.get("kernel_limits_verified") is True
                        and self.limits.mode == "systemd"
                    ),
                )
        # Keep the bounded, credential-free result for crash diagnosis; remove
        # executable plans so they cannot be reused as a later launch authority.
        for name in ("claude", "plan.json"):
            (self.directory / name).unlink(missing_ok=True)
        if exception_type is not None and issubclass(exception_type, asyncio.CancelledError):
            return
        if outcome in _MESSAGES:
            raise WorkerResourceError(_MESSAGES[outcome]) from None
        if exception_type is None and outcome not in {
            "completed",
            "terminated",
            "disconnected",
            "descendants_remaining",
        }:
            raise WorkerResourceError("SDK worker termination could not be verified.")
