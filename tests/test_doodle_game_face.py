from __future__ import annotations

from pathlib import Path
import struct


ROOT = Path(__file__).parents[1]
ASSETS = ROOT / "bridge/static/facepacks/doodle-game"
EMOTIONS = (
    "neutral",
    "happy",
    "laughing",
    "sad",
    "surprised",
    "thinking",
    "angry",
    "loving",
    "sleepy",
)


def test_doodle_face_assets_are_complete_and_320x240():
    for emotion in EMOTIONS:
        payload = (ASSETS / f"{emotion}.png").read_bytes()
        assert payload[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", payload[16:24])
        assert (width, height) == (320, 240)


def test_dashboard_uses_every_doodle_face_asset():
    dashboard = (ROOT / "bridge/templates/dashboard.html").read_text()
    face_fragment = (ROOT / "bridge/templates/face.html").read_text()
    assert "/ui/static/facepacks/doodle-game/{{ doodle_faces[e] }}.png" in dashboard
    for emotion in EMOTIONS:
        assert emotion in dashboard
        assert emotion in face_fragment
