# MemoryAtoms privacy policy

MemoryAtoms is a plugin that runs on your own computer inside Claude Code. This page says what it does with your data.

## What the author collects

Nothing. The plugin has no account, no telemetry and no analytics. It does not send your cards, prompts, code or usage to the author or to any server the author runs. The author cannot see anything you do with it.

## What stays on your computer

- Your memory cards, their indexes and logs live in the data folder you chose at install (default `~/.memoryatoms`).
- Project cards live in `<project>/.claude/memory/` once you set up project memory for that project.
- To build cards from a session, the plugin reads that session's Claude Code transcript on your disk. The text is processed locally.

## When data leaves your computer

Only when you turn one of these on yourself:

- **Remote LLM.** If you point the plugin at an Ollama server on another machine, session text used for card extraction is sent to that server. With the default settings the model runs locally.
- **Sync.** If you run the sync setup, your cards are committed and pushed to the git remote you configure.
- **Organization layer.** If you connect a shared organization memory repository, cards you write to that layer are pushed to it.
- **Vector search setup.** If you run `/memoryatoms:setup-vector`, it downloads Python packages, Ollama and a model from their official sources. Each step asks first. Nothing about you is uploaded.

Anything you type to Claude is handled by Claude Code and Anthropic under Anthropic's own terms and privacy policy, not by this plugin.

## Secrets

Cards you mark as secret are kept in a folder that sync skips and vector indexing skips. Passwords you enter in the install dialog are stored by Claude Code in your system's credential store.

## Removing your data

Uninstalling the plugin removes its program files. Your data folder is yours and stays until you delete it.

## Questions

Open an issue on the plugin's GitHub repository.
