#!/usr/bin/env python3
"""Validate profile packages and deterministically build catalog/index.json."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from profile_store import (  # noqa: E402
    ProfileStore,
    ProfileStoreError,
    strict_json_loads,
)


def build() -> dict[str, object]:
    packages_dir = ROOT / "catalog" / "packages"
    validator = ProfileStore(
        cache_dir=ROOT / ".profile-catalog-validation",
        schemas_dir=ROOT / "schemas",
        bundled_index=ROOT / "catalog" / "index.json",
        index_url="test://catalog/index.json",
        fetcher=lambda _url, _limit: b"",
        allow_test_urls=True,
    )
    games: list[dict[str, object]] = []
    for path in sorted(packages_dir.glob("*.json")):
        raw = path.read_bytes()
        if len(raw) > 512_000:
            raise ProfileStoreError(f"{path}: package exceeds 512000 bytes")
        package = validator.validate_package(strict_json_loads(raw), expected_id=path.stem)
        profile = package["profile"]
        games.append(
            {
                "id": package["id"],
                "name": profile["name"],
                "description": profile.get("description", ""),
                "version": package["version"],
                "packagePath": f"packages/{path.name}",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "sizeBytes": len(raw),
                "tags": package["tags"],
            }
        )
    if not games:
        raise ProfileStoreError("catalog/packages contains no profile packages")
    index: dict[str, object] = {
        "schemaVersion": 1,
        "repository": "https://github.com/Stormxftw/hermes-game-host-console",
        "games": games,
    }
    validator.validate_index(index)
    return index


def encoded(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if catalog/index.json is stale")
    args = parser.parse_args()
    try:
        expected = encoded(build())
    except (OSError, UnicodeError, json.JSONDecodeError, ProfileStoreError) as exc:
        print(f"profile catalog validation failed: {exc}", file=sys.stderr)
        return 1
    destination = ROOT / "catalog" / "index.json"
    if args.check:
        actual = destination.read_bytes() if destination.is_file() else b""
        if actual != expected:
            print("catalog/index.json is stale; run scripts/build-profile-catalog.py", file=sys.stderr)
            return 1
        print(f"verified {len(json.loads(expected)['games'])} profile packages and catalog/index.json")
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(expected)
    print(f"wrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
