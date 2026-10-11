"""Shutdown fits Cloud Run's grace (FD20).

Cloud Run sends SIGKILL ten seconds after SIGTERM
(docs.cloud.google.com/run/docs/container-contract, Shutdown). uvicorn's
graceful window plus the worker join must end with room left for the rest of
the app's shutdown.
"""

from __future__ import annotations

from caos import serve

CLOUD_RUN_GRACE_SECONDS = 10
SHUTDOWN_MARGIN_SECONDS = 2


def test_drain_fits_cloud_run_grace() -> None:
    drain = serve.GRACEFUL_SECONDS + serve.LIMIT_JOIN_SECONDS
    assert drain <= CLOUD_RUN_GRACE_SECONDS - SHUTDOWN_MARGIN_SECONDS
