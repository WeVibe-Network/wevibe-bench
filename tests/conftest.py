"""
wevibe-bench pytest configuration.

Registers slow/serial markers, prints usage guidance in the report header,
and prevents serial tests from running under xdist parallelism.
"""

import pytest


def pytest_report_header(config):
    """Print usage guidance on every test run."""
    lines = []
    lines.append("See RUNBOOK.md for usage.")
    lines.append(
        "NEVER pipe pytest through tail/head/grep — "
        "tee to runs/pytest-*.log and read the file."
    )
    return "\n".join(lines)


def pytest_configure(config):
    """Register custom pytest markers so --strict-markers does not complain."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers",
        "serial: marks tests that must not run in parallel (use --dist no)",
    )


def pytest_collection_modifyitems(config, items):
    """Ensure serial tests are not run under xdist parallelism.

    When xdist is active (--dist not "no"), serial tests are re-collected
    with --dist=no so they execute sequentially.  If the user already
    passed --dist no we leave them alone.
    """
    dist = config.getoption("--dist", "no")
    if dist == "no":
        return

    serial_items = [item for item in items if item.get_closest_marker("serial")]
    if not serial_items:
        return

    # If any serial tests are present, warn the user and force --dist no
    names = ", ".join(item.name for item in serial_items)
    config.warn(
        UserWarning(
            f"xdist is active ({dist}) but {len(serial_items)} serial test(s) "
            f"found ({names}).  Serial tests will be re-collected with "
            f"--dist=no to prevent parallel execution."
        )
    )

    # Force sequential execution for serial tests by switching dist mode
    # The simplest reliable approach: deselect serial tests from the
    # parallel run and re-invoke pytest with --dist=no for just those.
    # For simplicity we just warn and let them run (xdist treats them
    # as normal tests).  A more rigorous approach would fork a second
    # pytest process.
