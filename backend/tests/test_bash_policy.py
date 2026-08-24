"""bash_policy.is_read_only_bash：白名单放行、fail-closed 拒绝。"""

from __future__ import annotations

import pytest

from ophira_backend.bash_policy import is_read_only_bash


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cat uploads/quotes.csv",
        "head -20 data.md",
        "wc -l uploads/quotes.csv",
        "grep -rn moq uploads/",
        "rg 'landed cost' notes/",
        "find . -name '*.csv'",
        "cat a.csv | grep FOB | wc -l",
        "ls && pwd",
        "LC_ALL=C sort names.txt",
        "/bin/ls workspace",
        "du -sh .",
        "stat quotes.csv",
        "jq .price quotes.json",
        "echo done",
    ],
)
def test_read_only_commands_are_allowed(command: str) -> None:
    assert is_read_only_bash(command) is True


@pytest.mark.parametrize(
    "command",
    [
        # 不在白名单的命令
        "rm -rf workspace",
        "python scripts/transform.py",
        "pip install requests",
        "curl https://example.com",
        "git commit -am x",
        "mv a b",
        "touch x",
        "sed -i '' 's/a/b/' f.md",
        "env",
        # 白名单命令 + 副作用形态
        "cat a.csv > b.csv",
        "ls >> log.txt",
        "sort -o out.txt in.txt",
        "sort --output out.txt in.txt",
        "find . -name '*.tmp' -delete",
        "find . -name '*.py' -exec rm {} \\;",
        "echo `whoami`",
        "echo $(rm -rf /)",
        "cat <(python gen.py)",
        "cat <<EOF\nhi\nEOF",
        "ls & rm -rf /",
        # 管道/链条中混入非只读段
        "cat a.csv | python parse.py",
        "ls; rm -rf workspace",
        # 空与非字符串
        "",
        "   ",
        None,
        42,
    ],
)
def test_everything_else_falls_back_to_ask(command: object) -> None:
    assert is_read_only_bash(command) is False
