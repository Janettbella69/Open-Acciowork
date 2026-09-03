import pytest

from ophira_backend.destructive import is_destructive_delete


@pytest.mark.parametrize(
    "command",
    [
        "find . -type f -delete",
        "printf done;find . -type f -delete",
        "python -c \"from pathlib import Path; Path('x').unlink()\"",
        "python3.11 -c \"import os; os.remove('x')\"",
        "/bin/rm file.txt",
        "printf ready && /bin/rm file.txt",
        "git clean -fd",
        "git -C workspace clean -d -f",
        "sh -c 'rm file.txt'",
        "printf '%s\\0' a.txt | xargs -0 /bin/rm",
        "powershell -Command 'Remove-Item report.txt'",
    ],
)
def test_common_shell_delete_forms_require_confirmation(command: str) -> None:
    assert is_destructive_delete("Bash", {"command": command}) is True


@pytest.mark.parametrize(
    "command",
    [
        "find . -type f -print",
        "git clean -nd",
        "python -c \"print('safe')\"",
        "sh -c 'printf safe'",
        "ls workspace",
    ],
)
def test_non_deleting_commands_are_not_misclassified(command: str) -> None:
    assert is_destructive_delete("Bash", {"command": command}) is False
