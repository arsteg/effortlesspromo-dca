"""Console entry point (``effortlesspromo-dca``, declared in pyproject [project.scripts]).

Subcommands:
- ``serve`` (default, also the bare ``effortlesspromo-dca``) — run the ASGI app under uvicorn. Bind
  host/port come from env (``EFFORTLESSPROMO_DCA_HOST`` / ``_PORT``), everything else from config.
- ``seed`` — DCA-009 development-only: seed the two isolated synthetic test businesses (A & B).
  Idempotent; refuses under production configuration.
- ``worker`` — DCA-109: run the durable-job worker PROCESS (claim → maintenance/schedule ticks →
  drive lifecycle), with graceful SIGINT/SIGTERM shutdown. Gated behind ``worker_enabled`` (OFF by
  default). Handler EXECUTION stays a disabled future capability — the worker never runs a handler or
  sends; it parks claimed jobs safely. ``Ctrl-C`` stops new claims and relinquishes held work.

Configuration always comes from EFFORTLESSPROMO_DCA_* env vars / .env.
"""

from __future__ import annotations

import argparse
import os


def _serve() -> None:
    import uvicorn

    uvicorn.run(
        "app.asgi:app",
        host=os.environ.get("EFFORTLESSPROMO_DCA_HOST", "127.0.0.1"),
        port=int(os.environ.get("EFFORTLESSPROMO_DCA_PORT", "8100")),
    )


def _seed() -> None:
    """Build the DCA-owned engine from config and run the idempotent dev seed, then report."""
    from app.core.config import get_settings, validate_required
    from app.db.base import Base, make_engine, make_session_factory
    from app.db.seed import SeedRefused, seed_test_businesses

    settings = get_settings()
    validate_required(settings)
    engine = make_engine(settings.database_url or "")
    if settings.auto_create_schema:
        Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory() as session:
            result = seed_test_businesses(session, settings)
    except SeedRefused as exc:
        raise SystemExit(f"seed refused: {exc}") from exc
    finally:
        engine.dispose()
    print(
        "seeded test businesses: "
        f"{result['businesses']} (created={result['created']}, updated={result['updated']})"
    )


def _worker() -> None:
    """Run the DCA-109 durable-job worker loop with graceful SIGINT/SIGTERM shutdown.

    Gated behind ``worker_enabled`` (OFF by default — the worker loop is a config-gated capability).
    Handler EXECUTION stays disabled: the worker drives the claim/lease/park lifecycle mechanics and the
    recovery/schedule-maintenance ticks, but never runs a handler, does AI/network I/O or sends. A stop
    signal stops new claims and relinquishes any held work back to the queue.
    """
    import signal

    from app.core.config import get_settings, validate_required
    from app.db.base import Base, make_engine, make_session_factory
    from app.jobs.worker import DurableJobWorker

    settings = get_settings()
    validate_required(settings)
    if not settings.worker_enabled:
        raise SystemExit(
            "durable-job worker is disabled: set EFFORTLESSPROMO_DCA_WORKER_ENABLED=true to run it "
            "(it drives the lifecycle only - handler execution stays a disabled future capability)"
        )

    engine = make_engine(settings.database_url or "")
    if settings.auto_create_schema:
        Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    worker = DurableJobWorker(factory, settings)

    def _graceful(_signum: int, _frame: object) -> None:
        worker.request_shutdown()

    # Install signal handlers only when we own the main thread (a non-main thread / embedded host may
    # not permit it). A missing handler simply means Ctrl-C raises KeyboardInterrupt instead.
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", signal.SIGINT)):
        try:
            signal.signal(sig, _graceful)
        except (ValueError, OSError):  # pragma: no cover - platform/thread dependent
            pass

    print(
        f"DCA durable-job worker starting (id={worker.worker_id}); "
        "Ctrl-C to stop (handler execution is a disabled future capability - nothing is sent)"
    )
    try:
        passes = worker.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        worker.request_shutdown()
        passes = 0
    finally:
        engine.dispose()
    print(f"DCA durable-job worker stopped after {passes} pass(es)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="effortlesspromo-dca", description="EffortlessPromo Data Concierge Agent"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the ASGI app under uvicorn (default)")
    sub.add_parser("seed", help="dev-only: seed two isolated synthetic test businesses (A & B)")
    sub.add_parser(
        "worker", help="run the DCA-109 durable-job worker loop (gated; execution disabled)"
    )
    args = parser.parse_args(argv)

    if args.command == "seed":
        _seed()
    elif args.command == "worker":
        _worker()
    else:  # None (bare invocation) or "serve"
        _serve()


if __name__ == "__main__":
    main()
