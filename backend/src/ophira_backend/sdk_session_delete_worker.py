"""Credential-free subprocess using only public, pinned SDK session functions.

Do not import application settings or mutate the API process's environment.
The caller holds the data operation lease and validates the exact transcript tree.
"""

import json
import sys
from pathlib import Path

from claude_agent_sdk import delete_session, get_session_info


def main():
    operation, session_id, directory = sys.argv[1:]
    if operation not in {"inspect", "delete"}:
        raise ValueError("unsupported session operation")
    info = get_session_info(session_id, directory=directory)
    if info is None:
        print(json.dumps({"found": False}))
        return
    if info.cwd is None or Path(info.cwd).resolve() != Path(directory).resolve():
        raise ValueError("session working directory mismatch")
    if operation == "delete":
        delete_session(session_id, directory=directory)
    print(json.dumps({"found": True}))


if __name__ == "__main__":
    main()
