"""Rate limiting for agent provider calls.

``build_limiter()`` is the only entry point the rest of the repo uses. It
resolves a name to an implementation:

* ``noop`` -- the built-in do-nothing limiter, and the starting state.
* anything else -- imported as ``limiter.<name>``, whose ``build()`` function is
  called with no arguments and must return a :class:`~limiter.base.Limiter`.

So dropping ``limiter/mine.py`` with a ``build()`` in it and running
``LIMITER=mine make loadtest`` is all the wiring you need.
"""

from __future__ import annotations

import importlib
import os

from limiter.base import Limiter, NoopLimiter

__all__ = ["Limiter", "NoopLimiter", "build_limiter"]


def build_limiter(name: str | None = None) -> Limiter:
    """Construct the limiter named by ``name``, or by ``$LIMITER``."""
    name = (name or os.environ.get("LIMITER") or "noop").strip().lower()

    if name == "noop":
        return NoopLimiter()

    try:
        module = importlib.import_module(f"limiter.{name}")
    except ModuleNotFoundError as exc:
        raise ValueError(
            f"unknown limiter {name!r}: expected a module at limiter/{name}.py"
        ) from exc

    builder = getattr(module, "build", None)
    if builder is None:
        raise ValueError(f"limiter/{name}.py must define a build() function")

    return builder()
