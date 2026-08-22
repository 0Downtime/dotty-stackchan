import importlib.util
from pathlib import Path


ROOT = Path(__file__).parent.parent
SPEC = importlib.util.spec_from_file_location(
    "dotty_text_utils_catalog", ROOT / "custom-providers/textUtils.py"
)
TEXT_UTILS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TEXT_UTILS)


EXPECTED_IDS = (
    "neutral", "happy", "laughing", "funny", "sad", "angry", "crying",
    "loving", "embarrassed", "surprised", "shocked", "thinking", "winking",
    "cool", "relaxed", "delicious", "kissy", "confident", "sleepy", "silly",
    "confused",
)


def test_canonical_catalog_has_21_unique_faces():
    assert TEXT_UTILS.FACE_IDS == EXPECTED_IDS
    assert len(TEXT_UTILS.FACE_EMOJI_BY_ID) == 21
    assert len(set(TEXT_UTILS.CANONICAL_EMOJIS)) == 21


def test_legacy_aliases_resolve_to_canonical_faces():
    assert {emoji: TEXT_UTILS.EMOJI_MAP[emoji] for emoji in TEXT_UTILS.LEGACY_EMOJI_ALIASES} == {
        "😊": "happy", "😢": "sad", "😮": "surprised", "😐": "neutral",
    }
    assert set(TEXT_UTILS.LEGACY_EMOJI_ALIASES) <= set(TEXT_UTILS.ALLOWED_EMOJIS)


def test_prompt_and_dashboard_catalog_parity():
    suffix = TEXT_UTILS.build_turn_suffix(False)
    assert all(emoji in suffix for emoji in TEXT_UTILS.ALLOWED_EMOJIS)
    dashboard = (ROOT / "bridge/dashboard.py").read_text()
    template = (ROOT / "bridge/templates/dashboard.html").read_text()
    for emoji, face_id in TEXT_UTILS.FACE_CATALOG:
        assert emoji in dashboard and emoji in template
        assert face_id in dashboard


def test_direct_admin_route_is_validated_and_registered():
    server = (ROOT / "custom-providers/xiaozhi-patches/http_server.py").read_text()
    assert '"/xiaozhi/admin/set-emotion"' in server
    assert "FACE_EMOJI_BY_ID.get(emotion)" in server
    assert "send_serialized" in server
    assert 'status=400' in server
