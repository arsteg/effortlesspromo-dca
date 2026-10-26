"""Console entry point (``effortlesspromo-dca``, declared in pyproject [project.scripts]).

Subcommands:
- ``serve`` (default, also the bare ``effortlesspromo-dca``) — run the ASGI app under uvicorn. Bind
  host/port come from env (``EFFORTLESSPROMO_DCA_HOST`` / ``_PORT``), everything else from config.
- ``seed`` — DCA-009 development-only: seed the two isolated synthetic test businesses (A & B).
  Idempotent; refuses under production configuration.

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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="effortlesspromo-dca", description="EffortlessPromo Data Concierge Agent"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the ASGI app under uvicorn (default)")
    sub.add_parser("seed", help="dev-only: seed two isolated synthetic test businesses (A & B)")
    args = parser.parse_args(argv)

    if args.command == "seed":
        _seed()
    else:  # None (bare invocation) or "serve"
        _serve()


if __name__ == "__main__":
    main()
