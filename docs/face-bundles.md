# Face and voice bundles

Dotty's dashboard selects one preinstalled firmware face pack and one requested
voice profile per device. Desired state is stored atomically in
`DOTTY_FACE_BUNDLE_STATE` (default
`/var/lib/dotty-bridge/state/face-bundles.json`). The file stores only pack,
profile, status, and timestamp fields; credentials remain environment-managed.

The four v1 bundles are Classic Dotty, CRT Pixel Buddy, Aussie Host, and Kid
Bot. Classic and CRT use native LVGL renderers. Aussie Host and Kid Bot use
320x240 animated GIF renderers. The dashboard calls:

- `GET /ui/face-bundles`
- `POST /ui/actions/face-bundle/preview`
- `POST /ui/actions/face-bundle/apply`

The bridge forwards face changes to
`POST /xiaozhi/admin/set-face-pack`, which calls the firmware MCP tool
`self.robot.set_face_pack`. The firmware confirms activation with a
`face_pack_changed` event. Until that event matches the desired pack, the
dashboard reports the selection as pending. When a device reconnects,
xiaozhi-server reads the shared desired record and reasserts the face.

`local-cori` keeps the existing local filtered pipeline. `realtime-marin`
lazily opens the configured Realtime route. Switching to local cancels and
closes an active Realtime session. Kid Mode always wins: it forces Local Cori,
Kid Bot may enable it, and changing to another pack never disables it. If
Realtime is disabled, uncredentialed, or unavailable, the requested face stays
active while Local Cori is effective and the requested profile remains stored
for a later retry.

V1 intentionally excludes runtime uploads, on-device selection, and
voice-triggered switching.
