---
name: changelog-debug
description: Manually roll _CHANGELOG.md when the hook fails to fire (normally automatic; this is for debugging). Hook 觸發失靈時手動滾動 _CHANGELOG.md。
disable-model-invocation: true
---

# /memoryatoms-changelog-roll:changelog-debug

Manually roll `_AIDocs/_CHANGELOG.md`: keep the newest N entries (default 8) and move the rest to `_CHANGELOG_ARCHIVE.md`.

The PostToolUse hook already detects writes to `_CHANGELOG.md` and triggers this automatically; this skill is normally only for debugging, forcing a roll, or setting `--keep` by hand.

## Usage

- `/memoryatoms-changelog-roll:changelog-debug`: default keep=8
- `/memoryatoms-changelog-roll:changelog-debug --dry-run`: only preview which entries would move
- `/memoryatoms-changelog-roll:changelog-debug --keep 12`: change the threshold
- `/memoryatoms-changelog-roll:changelog-debug --quiet`: silent

## Implementation

Run `python ${CLAUDE_PLUGIN_ROOT}/tools/changelog-roll.py` with the arguments. On failure the tool prints to stderr and exits 2 without touching any file.
