#!/usr/bin/env python3
"""File a diagnostics download under ``dumps/``, scrubbed, with an index row.

Real downloads are the only evidence for what is on a vehicle's bus, and every
one this project has learned from arrived as an attachment on an issue and then
lived in a temporary directory until it was lost. This puts them in the
repository instead, where ``tools/dump_bus.py`` and the tests can read them.

Scrubbing is not the same as the redaction the integration already does, and
that is the point of the tool. ``async_redact_data`` matches key *names*, so a
download written before ``discovery_keys`` was added to ``TO_REDACT`` carries
the panel's BLE address inside a ``repr`` string that no key name reaches --
the three dumps attached to issue #22 are all like that. Here every string is
searched for an address whatever key it sits under, and the file is refused
rather than written if one survives.

Serial numbers are kept on purpose: they pin hardware rather than a location,
and they are how two devices of one class are told apart after a re-pairing.

Usage:
    tools/import_dump.py DOWNLOAD --vehicle SLUG --state LABEL [--issue N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DUMPS = ROOT / "dumps"

# Redacted wherever they appear, at any depth. The same set the integration
# uses, so a download taken before a key was added to it comes out as if it
# had been taken after.
REDACT_KEYS = {
    "address",
    "name",
    "title",
    "unique_id",
    "muid",
    "uuid",
    "discovery_keys",
}

REDACTED = "**REDACTED**"

# A BLE address in any of the spellings seen in a download, and the panel's
# own name, whose last three bytes are the identity address.
PATTERNS = (
    re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b"),
    re.compile(r"\b(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}\b"),
    re.compile(r"iNetX-[0-9A-Fa-f]{6}"),
)


def _strings(value: Any) -> Any:
    """Hunt an address down inside any string, whatever key it sits under."""
    if isinstance(value, dict):
        return {k: _strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strings(v) for v in value]
    if isinstance(value, str):
        for pattern in PATTERNS:
            value = pattern.sub(REDACTED, value)
    return value


def _by_key(value: Any, keys: set[str] = REDACT_KEYS) -> Any:
    """Redact by key name, at any depth."""
    if isinstance(value, dict):
        return {
            k: REDACTED if k in keys else _by_key(v, keys) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_by_key(v, keys) for v in value]
    return value


def scrub(download: Any) -> Any:
    """Redact the config entry by key name; the bus only by what strings say.

    Key names are only safe to redact inside the config entry. On the bus they
    are the panel's own vocabulary: a timer publishes its label as ``name`` --
    "23 °C" on the van, which is evidence and is quoted in the docs -- and
    redacting every ``name`` at any depth silently deletes it. So the bus is
    left as it arrived apart from the string hunt, which is what would catch an
    address there.
    """
    download = _strings(download)
    if not isinstance(download, dict):
        return download
    # A downloaded file wraps the integration's return value in ``data``; the
    # return value itself does not. Both turn up in an issue.
    if "entry" in download:
        return dict(download) | {"entry": _by_key(download["entry"])}
    data = download.get("data")
    if isinstance(data, dict) and "entry" in data:
        return dict(download) | {"data": dict(data) | {"entry": _by_key(data["entry"])}}
    return download


def survivors(blob: str) -> list[str]:
    """Anything address-shaped left in the finished file."""
    return sorted({m for p in PATTERNS for m in p.findall(blob)})


def describe(download: dict) -> dict[str, Any]:
    """The few fields the index row is built from."""
    data = download.get("data", download)
    state = data.get("state") or {}
    bus = data.get("bus") or {}
    devices = bus.get("devices") or state.get("device_params") or {}
    return {
        "ha": (download.get("home_assistant") or {}).get("version"),
        "integration": (download.get("integration_manifest") or {}).get("version"),
        "addresses": sorted(devices),
        "shape": "bus" if bus else ("per-device" if state.get("device_params") else "flat"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("download", type=Path)
    parser.add_argument("--vehicle", required=True, help="directory under dumps/")
    parser.add_argument("--state", required=True, help="what the panel was doing")
    parser.add_argument("--issue", help="issue number it came from")
    args = parser.parse_args()

    download = json.loads(args.download.read_text())
    cleaned = scrub(download)
    blob = json.dumps(cleaned, indent=1, ensure_ascii=False)

    left = survivors(blob)
    if left:
        print(f"refusing to write: {len(left)} address-shaped strings survived "
              f"scrubbing: {', '.join(left)}", file=sys.stderr)
        return 1

    out = DUMPS / args.vehicle / f"{args.state}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(blob + "\n")

    meta = describe(cleaned)
    print(f"wrote {out.relative_to(ROOT)}")
    print(
        f"| `{args.state}` | {meta['shape']} | "
        f"{', '.join(meta['addresses']) or '--'} | "
        f"{meta['integration']} / HA {meta['ha']} | "
        f"{('#' + args.issue) if args.issue else '--'} |"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
