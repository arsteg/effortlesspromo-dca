"""Console entry point (``effortlesspromo-dca``, declared in pyproject [project.scripts]).

Runs the ASGI app under uvicorn. Configuration still comes from EFFORTLESSPROMO_DCA_* env vars /
.env — the CLI only chooses bind host/port (env-overridable, sane local defaults).
"""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    uvicorn.run(
        "app.asgi:app",
        host=os.environ.get("EFFORTLESSPROMO_DCA_HOST", "127.0.0.1"),
        port=int(os.environ.get("EFFORTLESSPROMO_DCA_PORT", "8100")),
    )


if __name__ == "__main__":
    main()
