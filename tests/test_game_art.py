import hashlib
import json
from pathlib import Path


ART_ROOT = Path(__file__).parents[1] / "assets" / "game-art"
MANIFEST_PATH = ART_ROOT / "manifest.json"


def _webp_dimensions(content: bytes) -> tuple[int, int]:
    assert content[:4] == b"RIFF"
    assert content[8:12] == b"WEBP"
    assert content[12:16] == b"VP8 "
    assert content[23:26] == b"\x9d\x01\x2a"
    return (
        int.from_bytes(content[26:28], "little") & 0x3FFF,
        int.from_bytes(content[28:30], "little") & 0x3FFF,
    )


def test_packaged_game_art_is_rights_cleared_bounded_and_integrity_checked():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["schemaVersion"] == 1
    assert manifest["assets"]

    seen_game_ids: set[str] = set()
    for entry in manifest["assets"]:
        game_id = entry["gameId"]
        assert game_id not in seen_game_ids
        seen_game_ids.add(game_id)
        assert entry["repoPackagingRecommendation"] == "allow"
        assert entry["licenseSpdx"]
        assert entry["sourceKind"] in {
            "generated-original",
            "community-permissive",
            "publisher-explicit",
        }
        assert entry["mediaType"] == "image/webp"

        relative = Path(entry["file"])
        assert not relative.is_absolute()
        assert ".." not in relative.parts
        asset = (ART_ROOT / relative).resolve()
        asset.relative_to(ART_ROOT.resolve())
        content = asset.read_bytes()
        assert len(content) == entry["sizeBytes"]
        assert len(content) <= 512_000
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        assert _webp_dimensions(content) == (entry["width"], entry["height"])
        assert entry["width"] == 1600
        assert entry["height"] == 517


def test_every_packaged_hero_has_a_manifest_entry():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    declared = {entry["file"] for entry in manifest["assets"]}
    packaged = {
        str(path.relative_to(ART_ROOT))
        for path in ART_ROOT.glob("*/hero.webp")
    }
    assert packaged == declared
