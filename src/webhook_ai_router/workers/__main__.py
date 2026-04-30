"""arq worker entrypoint.

Run with::

    python -m webhook_ai_router.workers
"""

from __future__ import annotations

from arq import run_worker

from webhook_ai_router.workers.tasks import WorkerSettings


def main() -> None:
    # WorkerSettings structurally satisfies arq's WorkerSettingsBase Protocol;
    # mypy doesn't infer that because we don't subclass it.
    run_worker(WorkerSettings)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
