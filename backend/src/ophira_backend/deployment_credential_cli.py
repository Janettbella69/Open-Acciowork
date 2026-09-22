"""Local root-key maintenance without constructing the application or starting jobs."""

import argparse
import getpass
import json
import os
import warnings
from pathlib import Path

from .credential_archive import MAX_ARCHIVE_BYTES
from .credential_models import CredentialKind, CredentialReview
from .deployment_credentials import (
    DeploymentCredentialId,
    DeploymentCredentialRegistry,
    DeploymentCredentialSettings,
)
from .maintenance import DataDirectoryBusyError, data_directory_lease
from .secret_vault import CredentialChangedError
from .skill_proposal_files import directory_fd, read_relative


def _private_prompt(prompt):
    # Never silently fall back to echoed input or expose secrets through arguments.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt)


def _export_file(path, data):
    with directory_fd(path.absolute().parent) as parent:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.fsync(parent)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Deployment-owner credential maintenance. Stop all workers before mutations.",
    )
    parser.add_argument(
        "--data-dir", type=Path, required=True, help="Existing application data root"
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Explicit deployment .env; defaults to process environment only",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Print review metadata, never secret values")
    for name in ("export", "set", "delete", "restore"):
        command = commands.add_parser(name)
        command.add_argument("identifier", choices=list(DeploymentCredentialId))
        command.add_argument("--expected-revision", required=True)
        if name == "export":
            command.add_argument("--output", required=True, type=Path)
        if name == "restore":
            command.add_argument("--input", required=True, type=Path)
        if name in {"delete", "restore"}:
            command.add_argument(f"--confirm-{name}", required=True, action="store_true")
    args = parser.parse_args(argv)
    try:
        root = args.data_dir.resolve(strict=True)
        if args.env_file is not None and not args.env_file.is_file():
            raise ValueError("Missing environment file")
        settings = DeploymentCredentialSettings(
            _env_file=args.env_file,
            data_dir=root,
        )
        if args.command != "list":
            CredentialReview(expected_revision=args.expected_revision)
        # Exclusive mutations cannot race any cooperating live worker or cold backup.
        with data_directory_lease(root, exclusive=args.command in {"set", "delete", "restore"}):
            registry = DeploymentCredentialRegistry(settings)
            if args.command == "list":
                result = {
                    "data_dir": str(root),
                    "tenant_id": settings.trusted_gateway_tenant_id,
                    "credentials": [registry.describe(key) for key in DeploymentCredentialId],
                }
            else:
                identifier, revision = args.identifier, args.expected_revision
                if args.command == "export":
                    password = _private_prompt("New archive password (12+ characters): ")
                    if password != _private_prompt("Repeat archive password: "):
                        raise ValueError("Passwords differ")
                    _export_file(args.output, registry.export(identifier, revision, password))
                    result = {"exported": True, "kind": CredentialKind.DEPLOYMENT, "id": identifier}
                elif args.command == "delete":
                    registry.delete(identifier, revision)
                    result = registry.describe(identifier)
                elif args.command == "set":
                    secret = _private_prompt("Replacement credential (32–512 characters): ")
                    if secret != _private_prompt("Repeat replacement credential: "):
                        raise ValueError("Credentials differ")
                    registry.save(identifier, secret, revision)
                    result = registry.describe(identifier)
                else:
                    data = read_relative(
                        args.input.absolute().parent, args.input.name, MAX_ARCHIVE_BYTES
                    )
                    registry.restore(
                        identifier, revision, data, _private_prompt("Archive password: ")
                    )
                    result = registry.describe(identifier)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except DataDirectoryBusyError:
        parser.exit(1, "Data directory is busy. Stop all application workers and retry.\n")
    except CredentialChangedError:
        parser.exit(
            1, "Credential changed. List and review its current revision before retrying.\n"
        )
    except (OSError, ValueError, EOFError, getpass.GetPassWarning, KeyboardInterrupt):
        parser.exit(
            1,
            "Credential operation failed. Check metadata, paths and private input; "
            "list current state before retrying. No secret values were printed.\n",
        )


if __name__ == "__main__":
    raise SystemExit(main())
