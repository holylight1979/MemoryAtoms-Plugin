# memoryatoms-changelog-roll

Rolls old rows of _CHANGELOG.md into _CHANGELOG_ARCHIVE.md in the background once it passes a row threshold; /changelog-debug runs it by hand.

An extension of the MemoryAtoms plugin: installing it also enables `memoryatoms`, which it needs.

Install: `claude plugin install memoryatoms-changelog-roll@memoryatoms`

## What it does

For projects that keep a `_CHANGELOG.md` table, it watches for edits to that file. Once the table grows past a row threshold, it moves the oldest rows into `_CHANGELOG_ARCHIVE.md` in the background, so the live changelog stays short. Nothing is deleted: rows only move between the two files.

## Settings

`changelog_auto_roll.threshold` in the MemoryAtoms `workflow/config.json` sets the row count (default 8). Set `enabled` to false to stop the automatic roll and run `/changelog-debug` by hand instead.
