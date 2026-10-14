"""Entry point: `uvicorn main:app`.

The application lives in the `fleet` package (see fleet/__init__.py for the layout); this
file only exposes the ASGI app under the name existing deploy commands expect.
"""
from fleet.api import app

__all__ = ["app"]
