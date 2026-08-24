"""Read-only Bash classification for the PreToolUse hook (FM-01/FM-02 mitigation).

口径（2026-08-23 用户拍板，对齐 Agent Harness 教材第 03/05/09 章）：bash 是 Agent
兜底预期外任务的手；只读探查（ls/cat/grep …）动作可逆、不碰生产数据，在沙箱内
自动放行；任何有副作用的命令仍强制 ask 且永不按 run 缓存。

分类器刻意保守（fail-closed）：识别不了的一律不算只读，回落到 ask——漏放的
代价只是多一张授权卡片，错放的代价是未审批的副作用，因此宁可漏放。
"""

from __future__ import annotations

import re

# 只读命令白名单：每个管道段的首个命令词必须整个落在这里。
# 刻意从紧；env/printenv 会打印 worker 凭据，永远不进这个名单。
READ_ONLY_BASH_COMMANDS = frozenset(
    {
        "basename",
        "cat",
        "column",
        "cut",
        "date",
        "df",
        "dirname",
        "du",
        "echo",
        "file",
        "find",
        "grep",
        "head",
        "jq",
        "ls",
        "md5",
        "nl",
        "printf",
        "pwd",
        "realpath",
        "rg",
        "shasum",
        "sort",
        "stat",
        "tail",
        "tree",
        "uname",
        "uniq",
        "wc",
        "which",
        "whoami",
    }
)

# 命令里出现任何一个即整体判非只读：输出重定向、命令/进程替换、heredoc。
# 普通输入重定向（wc -l < file）是读，不在此列。
_UNSAFE_PATTERN = re.compile(r">|`|\$\(|<\(|<<")

# 这些参数让白名单命令产生副作用。
_FORBIDDEN_ARGS: dict[str, frozenset[str]] = {
    "find": frozenset(
        {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
    ),
    "sort": frozenset({"-o", "--output"}),
}

_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||\n")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def is_read_only_bash(command: object) -> bool:
    """True only when every pipeline segment is a whitelisted read-only command."""
    if not isinstance(command, str) or not command.strip():
        return False
    if _UNSAFE_PATTERN.search(command):
        return False
    for segment in _SEGMENT_SPLIT.split(command):
        tokens = segment.strip().split()
        # 跳过前缀环境变量赋值（如 LC_ALL=C grep …）。
        while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            return False
        head = tokens[0].rsplit("/", 1)[-1]
        if head not in READ_ONLY_BASH_COMMANDS:
            return False
        # 拆分后残留的裸 & 意味着后台执行，判非只读。
        if any("&" in token for token in tokens):
            return False
        forbidden = _FORBIDDEN_ARGS.get(head)
        if forbidden and forbidden & set(tokens[1:]):
            return False
    return True
