"""Detect irreversible file-delete actions (FR-13 Cowork iron rule)."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePath
from typing import Any

from .constants import DELETE_BUILTIN_TOOLS

_DELETE_COMMANDS = {
    "del",
    "erase",
    "rd",
    "remove-item",
    "rm",
    "rmdir",
    "shred",
    "trash",
    "unlink",
}
_SHELL_COMMANDS = {"bash", "cmd", "dash", "fish", "powershell", "pwsh", "sh", "zsh"}
_INTERPRETER_COMMANDS = {"node", "perl", "php", "pypy", "pypy3", "python", "python3", "ruby"}
_INLINE_DELETE_PATTERN = re.compile(
    r"(?ix)(?:"
    r"\b(?:os\.)?(?:remove|removedirs|rmdir|unlink)\s*\(|"
    r"\bshutil\.rmtree\s*\(|"
    r"\bpath(?:lib)?(?:\.path)?\s*\([^)]*\)\s*\.\s*(?:rmdir|unlink)\s*\(|"
    r"\bfs\.(?:rm|rmdir|unlink)(?:sync)?\s*\(|"
    r"\bfile\.(?:delete|unlink)\s*\("
    r")"
)


def _executable_name(value: str) -> str:
    """Normalize `/bin/rm` and Windows paths without touching the filesystem."""
    return PurePath(value.replace("\\", "/")).name.casefold()


def _shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        # An incomplete quote must not turn a deletion into an automatic approval.
        return re.findall(r"[^\s|&;()]+", command)


def _contains_destructive_command(command: str, *, depth: int = 0) -> bool:
    if not command.strip() or depth > 3:
        return False
    tokens = _shell_tokens(command)
    names = [_executable_name(token) for token in tokens]

    # Direct removers also catches wrappers such as `sudo /bin/rm` and `xargs rm`.
    if any(name in _DELETE_COMMANDS for name in names):
        return True

    # `find ... -delete` and forced `git clean` remove files without invoking rm.
    if "find" in names and any(token.casefold() == "-delete" for token in tokens):
        return True
    for index, name in enumerate(names):
        if name != "git" or "clean" not in names[index + 1 :]:
            continue
        clean_index = names.index("clean", index + 1)
        clean_args = tokens[clean_index + 1 :]
        if any(
            arg == "--force" or (arg.startswith("-") and "f" in arg[1:].casefold())
            for arg in clean_args
        ):
            return True

    # Shell and interpreter one-liners hide the actual operation in a quoted argument.
    for index, name in enumerate(names):
        if name in _SHELL_COMMANDS:
            for flag in ("-c", "/c", "-command"):
                try:
                    code_index = next(
                        candidate
                        for candidate in range(index + 1, len(tokens))
                        if tokens[candidate].casefold() == flag
                    )
                except StopIteration:
                    continue
                if code_index + 1 < len(tokens) and _contains_destructive_command(
                    tokens[code_index + 1], depth=depth + 1
                ):
                    return True
        if name in _INTERPRETER_COMMANDS or re.fullmatch(
            r"(?:pypy|python)\d+(?:\.\d+)*", name
        ):
            inline_flags = {"-c", "-e"}
            for code_index in range(index + 1, len(tokens) - 1):
                if tokens[code_index].casefold() in inline_flags and _INLINE_DELETE_PATTERN.search(
                    tokens[code_index + 1]
                ):
                    return True
    return False


def is_destructive_delete(tool_name: str, tool_input: dict[str, Any] | None = None) -> bool:
    name = (tool_name or "").split("__")[-1]
    if name in DELETE_BUILTIN_TOOLS or tool_name in DELETE_BUILTIN_TOOLS:
        return True
    if name != "Bash" and tool_name != "Bash":
        return False
    command = str((tool_input or {}).get("command") or "")
    return _contains_destructive_command(command)
