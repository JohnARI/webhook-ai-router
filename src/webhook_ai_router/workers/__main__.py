"""arq worker entrypoint.

Run with::

    python -m webhook_ai_router.workers
"""

from __future__ import annotations

from arq import run_worker

from webhook_ai_router.workers.tasks import WorkerSettings


def main() -> None:
    # ``WorkerSettings`` structurally satisfies arq's ``WorkerSettingsBase``
    # Protocol — it has functions/redis_settings/on_startup/on_shutdown — but
    # mypy can't infer the structural match because we don't explicitly
    # subclass the Protocol (it lives in arq.typing and is intended as a
    # runtime-only marker). Drop the ignore once arq exposes a non-Protocol
    # base class to inherit from.
    run_worker(WorkerSettings)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
