"""``aura-runtime`` entrypoint."""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("aura_runtime.app:create_app", factory=True, host="0.0.0.0", port=8100)  # noqa: S104


if __name__ == "__main__":
    main()
