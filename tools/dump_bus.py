#!/usr/bin/env python3
"""Run the bus dump tool without importing Home Assistant.

``bus.py`` and everything it uses import by relative path, so they need to be
inside a package -- and ``custom_components/truma_inetx/__init__.py`` is the
integration's Home Assistant entry point, which cannot be imported without
Home Assistant installed. ``python3 -m custom_components.truma_inetx.bus``
would execute it and fail on the first line.

So this puts the package on ``sys.modules`` by hand, pointing at the same
directory, and loads ``bus`` out of it. The package ``__init__`` never runs;
nothing below it needs Home Assistant. It is the same trick the tests use, for
the same reason.

    ./tools/dump_bus.py --help
    ./tools/dump_bus.py diagnostics.json
    ./tools/dump_bus.py --live --identity ~/ha/config/.storage/truma_inetx_<id>

A live dump needs ``bleak``; reading a download needs nothing but the standard
library.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "custom_components" / "truma_inetx"


def _package(name: str, path: Path) -> None:
    module = types.ModuleType(name)
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    _package("truma_inetx", SRC)
    _package("truma_inetx.truma", SRC / "truma")
    bus = _load("truma_inetx.bus", SRC / "bus.py")
    return bus._main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
