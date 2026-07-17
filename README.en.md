# RiNG 🎤

[台灣華語](README.md) · **English**

[![PyPI](https://img.shields.io/pypi/v/ring-cli?label=PyPI)](https://pypi.org/project/ring-cli/)
[![Python](https://img.shields.io/pypi/pyversions/ring-cli)](https://pypi.org/project/ring-cli/)
[![License](https://img.shields.io/pypi/l/ring-cli)](LICENSE)

> **R**ealtime **I**nstance **N**otification **G**rid
> — one local board for all active agent-CLI sessions.

When several Claude Code, Codex, or local-model sessions are running, RiNG shows which ones are working, idle, or waiting for you. Sessions that need a response are sorted first.

```text
🎤 RiNG — 3 sessions on stage · 2 agent processes running

  🔴 maigo            12s  → waiting for permission
  🟢 pelican-osm       3s  → Edit
  🟡 commitizen        8m  turn finished, idle
```

## What It Does

- **One board for session state**: built-in sources for Claude Code, Codex, Ollama, and llama.cpp.
- **Puts waiting sessions first**: shows what 🔴 sessions need and sends desktop or phone notifications.
- **Returns you to the right terminal**: focuses tmux, iTerm2, Terminal.app, Neovim terminals, or Linux X11 windows from the TUI.
- **Avoids an extra context switch**: reply to permission requests in supported terminals and assign session names from the board.
- **Fits existing workflows**: status-bar output, JSON, a provider-neutral hook, and plugin extension points.

## Get Started in Three Steps

Requires Python 3.13+:

```sh
uv tool install 'ring-cli[tui]'
ring --watch
ring install-hooks
```

1. `ring --watch` opens the interactive board. Select a session and press `Enter` to return to its terminal.
2. `ring install-hooks` installs Claude Code and Codex hooks for precise 🔴 waiting states and system notifications.
3. Restart agent sessions after installing hooks. RiNG can then notify you even while its board is closed.

Use `pipx install 'ring-cli[tui]'` if you prefer pipx. Run `ring` for a one-shot snapshot.

## Common Operations

| Operation | Purpose |
|-----------|---------|
| `ring` | Print a snapshot of every current session |
| `ring --watch` | Open the continuously updating interactive TUI |
| `Enter` / `Space` | Return to the selected session's terminal |
| `p` | Reply to the selected session's permission request in place |
| `n` | Name the selected session |
| `ring doctor` | Check hooks, notifications, and terminal focus support |
| `ring --format oneline` | Produce a summary for tmux, SwiftBar, or waybar |

Zero-config mode works without hooks. Precise 🔴 waiting states, pending details, and immediate notifications require hooks. Restart Claude Code or Codex sessions after installing or updating them.

## Learn More

- [Full guide](https://lee-w.github.io/ring/guide/): commands, TUI controls, hooks, notifications, configuration, extensions, and privacy
- [Session states](https://lee-w.github.io/ring/session-states/): how RiNG derives 🔴/🟢/🟡/⚫
- [Contributing guide](CONTRIBUTING.md): development setup, tests, and PR checklist

RiNG supports macOS and Linux; Windows is not yet supported. By default it only reads local agent data and writes `~/.config/ring/`. Network access occurs only for ntfy or webhook notifiers you configure.

## License

MIT
