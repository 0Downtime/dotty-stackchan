---
title: Emoji → Expression Mapping
description: How emoji characters in LLM responses map to face animations on the StackChan.
---

# Emoji → Expression Mapping

Every LLM response starts with an emoji. The xiaozhi-server parses this
emoji and sends an emotion frame to the StackChan firmware, which renders
the corresponding face animation.

## Active mapping

The canonical ordered catalog is defined by `FACE_CATALOG` in
`custom-providers/textUtils.py`: 😶 neutral, 🙂 happy, 😆 laughing, 😂 funny,
😔 sad, 😠 angry, 😭 crying, 😍 loving, 😳 embarrassed, 😲 surprised, 😱
shocked, 🤔 thinking, 😉 winking, 😎 cool, 😌 relaxed, 🤤 delicious, 😘 kissy,
😏 confident, 😴 sleepy, 😜 silly, and 🙄 confused.

For backward compatibility, 😊 maps to happy, 😢 to sad, 😮 to surprised, and
😐 to neutral. New UI and admin clients should use the canonical catalog.

## Enforcement on the live PiVoiceLLM path

`build_turn_suffix()` requests one canonical emoji (legacy aliases are also
accepted) on every turn.
`PiVoiceLLM._enforce_leading_emoji()` then enforces the wire contract: it
preserves an allowed prefix or prepends neutral `😐` before TTS. Persona files
and xiaozhi's top-level `.config.yaml` prompt are not forwarded by PiVoiceLLM;
Pi runs with `--no-context-files`.

If the model omits the prefix, the neutral fallback is used. A newly allowed
emoji must be added consistently to `ALLOWED_EMOJIS`, `EMOJI_MAP`, and the
firmware mapping or it will not select the intended face.

## How to Add a New Emoji

See [docs/cookbook/add-emoji.md](cookbook/add-emoji.md).

## Where the Code Lives

| Component | File | What it does |
|-----------|------|-------------|
| Per-turn emoji + rules suffix | `custom-providers/textUtils.py` | `build_turn_suffix()` (appended on the live `PiVoiceLLM` path) |
| Leading-emoji enforcement | `custom-providers/pi_voice/pi_voice.py` | `_enforce_leading_emoji()` |
| Emoji → emotion | `custom-providers/textUtils.py` | `EMOJI_MAP` dict, `get_emotion()` |
| Emotion → face | StackChan firmware | Avatar renderer, expression assets |

## Deterministic UAT

Authenticated administrators can bypass the LLM and set a face directly with
`POST /xiaozhi/admin/set-emotion` and JSON
`{"emotion":"<catalog-id>","device_id":"<optional>"}`. The server validates
the ID, derives its canonical emoji, and emits the normal `llm` emotion frame.
