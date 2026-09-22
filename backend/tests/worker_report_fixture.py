"""Explicit synthetic process receipts for SDK message-mapping unit fixtures.

These clients never launch a process. Real supervisor and kernel acceptance tests
must use the executable instead of these helpers.
"""


def synthetic_worker_start(options):
    (options.cli_path.parent / "started.json").write_text(
        '{"mode":"native","kernel_limits_verified":false}',
    )


def synthetic_worker_finish(options):
    (options.cli_path.parent / "finished.json").write_text(
        '{"outcome":"completed","output_bytes":0,"exit_code":0}',
    )
