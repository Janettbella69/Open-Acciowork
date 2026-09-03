from pathlib import Path

import pytest

from ophira_backend.constants import (
    MemoryKind,
    MemoryOrigin,
    MemoryScope,
    MemorySensitivity,
    MemorySource,
    MemoryStatus,
)
from ophira_backend.memory import FileMemoryProvider, MemoryNotFoundError, gather_approved


def propose(memory: FileMemoryProvider, text: str = "偏好中文报告", **overrides):
    params = {
        "scope": MemoryScope.AGENT,
        "scope_id": "agent_1",
        "text": text,
        "kind": MemoryKind.PREFERENCE,
        "source": MemorySource.AGENT_PROPOSAL,
        "created_by": "agent_1",
    }
    params.update(overrides)
    return memory.propose(**params)


def test_agent_proposal_requires_approval_before_search(tmp_path: Path) -> None:
    memory = FileMemoryProvider(tmp_path)
    item = propose(memory)

    assert item.status == MemoryStatus.PROPOSED
    assert memory.search("中文") == []

    approved = memory.decide(item.id, "approve")
    assert approved.status == MemoryStatus.APPROVED
    assert approved.version == 2
    assert [found.id for found in memory.search("中文")] == [item.id]


def test_sensitive_proposal_is_never_auto_approved(tmp_path: Path) -> None:
    memory = FileMemoryProvider(tmp_path)
    item = propose(memory, sensitivity=MemorySensitivity.SENSITIVE, text="密钥偏好")

    assert item.status == MemoryStatus.PROPOSED
    assert memory.search("密钥") == []


def test_user_created_memory_is_immediately_approved(tmp_path: Path) -> None:
    memory = FileMemoryProvider(tmp_path)
    item = propose(memory, source=MemorySource.USER, created_by="user")

    assert item.status == MemoryStatus.APPROVED
    assert [found.id for found in memory.search("中文")] == [item.id]


def test_reject_and_disable_hide_memory(tmp_path: Path) -> None:
    memory = FileMemoryProvider(tmp_path)
    rejected = propose(memory, text="无效偏好")
    memory.decide(rejected.id, "reject")
    assert memory.search("无效") == []

    disabled = propose(memory, text="过时偏好")
    memory.decide(disabled.id, "approve")
    memory.decide(disabled.id, "disable")
    assert memory.search("过时") == []


def test_rollback_restores_prior_version(tmp_path: Path) -> None:
    memory = FileMemoryProvider(tmp_path)
    item = propose(memory)
    memory.decide(item.id, "approve")

    rolled_back = memory.rollback(item.id, 1)

    assert rolled_back.status == MemoryStatus.PROPOSED
    assert rolled_back.version == 3
    assert memory.search("中文") == []
    with pytest.raises(MemoryNotFoundError):
        memory.rollback(item.id, 99)


def test_owner_correction_creates_an_approved_version_and_can_rollback(
    tmp_path: Path,
) -> None:
    memory = FileMemoryProvider(tmp_path)
    item = propose(memory, text="旧的推断", kind=MemoryKind.LESSON)

    corrected = memory.correct(
        item.id,
        text="  用户确认的新事实  ",
        kind=MemoryKind.FACT,
        sensitivity=MemorySensitivity.SENSITIVE,
    )

    assert corrected.text == "用户确认的新事实"
    assert corrected.kind == MemoryKind.FACT
    assert corrected.sensitivity == MemorySensitivity.SENSITIVE
    assert corrected.source == MemorySource.USER
    assert corrected.origin == MemoryOrigin.OWNER
    assert corrected.status == MemoryStatus.APPROVED
    assert corrected.version == 2
    assert memory.search("新事实")[0].id == item.id

    rolled_back = memory.rollback(item.id, 1)
    assert rolled_back.text == "旧的推断"
    assert rolled_back.status == MemoryStatus.PROPOSED
    assert rolled_back.version == 3


def test_gather_approved_merges_scopes(tmp_path: Path) -> None:
    memory = FileMemoryProvider(tmp_path)
    user_item = propose(
        memory,
        scope=MemoryScope.USER,
        scope_id="default",
        source=MemorySource.USER,
        text="用户偏好",
    )
    agent_item = propose(memory, text="项目经验", kind=MemoryKind.LESSON)
    memory.decide(agent_item.id, "approve")
    propose(memory, text="未批准条目")

    merged = gather_approved(
        memory,
        [(MemoryScope.USER, "default"), (MemoryScope.AGENT, "agent_1")],
    )

    assert {item.id for item in merged} == {user_item.id, agent_item.id}


def test_agent_scope_approval_syncs_memory_md(tmp_path: Path) -> None:
    agents_root = tmp_path / "agents"
    core_dir = agents_root / "agent_1"
    core_dir.mkdir(parents=True)
    memory_md = core_dir / "MEMORY.md"
    memory_md.write_text("# 长期记忆\n\n用户手写的一行\n", encoding="utf-8")
    memory = FileMemoryProvider(tmp_path / "memory", agents_root=agents_root)

    item = propose(memory)
    assert "偏好中文报告" not in memory_md.read_text(encoding="utf-8")

    memory.decide(item.id, "approve")
    content = memory_md.read_text(encoding="utf-8")
    assert "偏好中文报告" in content
    assert f"<!-- mem:{item.id} -->" in content
    assert "用户手写的一行" in content

    memory.decide(item.id, "disable")
    content = memory_md.read_text(encoding="utf-8")
    assert "偏好中文报告" not in content
    assert "用户手写的一行" in content  # freehand edits survive sync


def test_user_created_agent_memory_syncs_immediately(tmp_path: Path) -> None:
    agents_root = tmp_path / "agents"
    (agents_root / "agent_1").mkdir(parents=True)
    memory = FileMemoryProvider(tmp_path / "memory", agents_root=agents_root)

    propose(memory, source=MemorySource.USER, created_by="user", text="直接录入")

    content = (agents_root / "agent_1" / "MEMORY.md").read_text(encoding="utf-8")
    assert "直接录入" in content
