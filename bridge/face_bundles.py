"""Shared face/voice bundle contract and durable per-device desired state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any


STATE_PATH = Path(
    os.environ.get(
        "DOTTY_FACE_BUNDLE_STATE",
        "/var/lib/dotty-bridge/state/face-bundles.json",
    )
)


@dataclass(frozen=True)
class FaceBundle:
    id: str
    display_name: str
    face_pack_id: str
    renderer: str
    requested_voice_profile: str
    enables_kid_mode: bool
    voice_label: str
    safety_label: str
    preview: str


BUNDLES: tuple[FaceBundle, ...] = (
    FaceBundle(
        "classic", "Classic Dotty", "classic", "native", "local-cori", False,
        "Local Cori", "Kid Mode is preserved", "/ui/static/facepacks/classic.svg",
    ),
    FaceBundle(
        "crt-pixel", "CRT Pixel Buddy", "crt-pixel", "native", "local-cori", False,
        "Local Cori", "Kid Mode is preserved", "/ui/static/facepacks/crt-pixel.png",
    ),
    FaceBundle(
        "aussie-host", "Aussie Host", "aussie-host", "gif", "realtime-marin", False,
        "Realtime Marin", "Kid Mode forces Local Cori", "/ui/static/facepacks/aussie-host.gif",
    ),
    FaceBundle(
        "kid-bot", "Kid Bot", "kid-bot", "gif", "local-cori", True,
        "Local Cori", "Enables Kid Mode", "/ui/static/facepacks/kid-bot.gif",
    ),
)
BUNDLE_BY_ID = {bundle.id: bundle for bundle in BUNDLES}


def effective_voice_profile(
    requested: str,
    *,
    kid_mode: bool,
    realtime_available: bool,
) -> tuple[str, str]:
    if kid_mode and requested == "realtime-marin":
        return "local-cori", "Kid Mode overrides Realtime with Local Cori."
    if requested == "realtime-marin" and not realtime_available:
        return "local-cori", "Realtime is unavailable; Local Cori is active and Marin will be retried later."
    return requested, ""


class FaceBundleStore:
    """Atomic JSON store. The bridge writes it; xiaozhi-server reads it."""

    def __init__(self, path: Path | str = STATE_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            if data.get("version") == 1 and isinstance(data.get("devices"), dict):
                return data
        except (OSError, ValueError, TypeError):
            pass
        return {"version": 1, "devices": {}}

    def read_all(self) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked()

    def get(self, device_id: str = "") -> dict[str, Any]:
        data = self.read_all()["devices"]
        key = (device_id or "default").strip() or "default"
        record = data.get(key)
        if not isinstance(record, dict) and key != "default":
            record = data.get("default")
        if not isinstance(record, dict):
            record = {
                "bundle_id": "classic",
                "face_pack_id": "classic",
                "requested_voice_profile": "local-cori",
                "active_face_pack_id": "",
                "pending": False,
                "warning": "",
            }
        return dict(record)

    def set_requested(
        self,
        device_id: str,
        bundle: FaceBundle,
        *,
        pending: bool,
        warning: str = "",
    ) -> dict[str, Any]:
        key = (device_id or "default").strip() or "default"
        with self._lock:
            data = self._read_unlocked()
            previous = data["devices"].get(key, {})
            record = {
                "bundle_id": bundle.id,
                "face_pack_id": bundle.face_pack_id,
                "requested_voice_profile": bundle.requested_voice_profile,
                "active_face_pack_id": previous.get("active_face_pack_id", ""),
                "pending": bool(pending),
                "warning": warning,
                "updated_at": int(time.time()),
            }
            data["devices"][key] = record
            self._write_unlocked(data)
            return dict(record)

    def mark_active(
        self,
        device_id: str,
        active_face_pack_id: str,
        *,
        success: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        key = (device_id or "default").strip() or "default"
        with self._lock:
            data = self._read_unlocked()
            record = dict(data["devices"].get(key) or self.get(key))
            record["active_face_pack_id"] = active_face_pack_id
            record["pending"] = not (
                success and active_face_pack_id == record.get("face_pack_id")
            )
            if reason:
                record["warning"] = reason
            record["updated_at"] = int(time.time())
            data["devices"][key] = record
            self._write_unlocked(data)
            return dict(record)

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", text=True,
        )
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(data, handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def public_bundle(bundle: FaceBundle) -> dict[str, Any]:
    return asdict(bundle)
