# RiNG Documentation

[![PyPI](https://img.shields.io/pypi/v/ring-cli?label=PyPI)](https://pypi.org/project/ring-cli/)
[![Python](https://img.shields.io/pypi/pyversions/ring-cli)](https://pypi.org/project/ring-cli/)
[![License](https://img.shields.io/pypi/l/ring-cli)](https://github.com/Lee-W/ring/blob/main/LICENSE)

> **R**ealtime **I**nstance **N**otification **G**rid
> — one local board for all active agent-CLI sessions.

RiNG puts Claude Code, Codex, and local-model sessions in one place, sorts work that needs your response first, and returns you directly to the original terminal.

```text
🎤 RiNG — 3 sessions on stage · 2 agent processes running

  🔴 maigo            12s  → waiting for permission
  🟢 pelican-osm       3s  → Edit
  🟡 commitizen        8m  turn finished, idle
```

## Who It Is For

- You run several Claude Code or Codex sessions at once.
- You want to distinguish working, idle, and waiting sessions.
- You want to return directly from the TUI to the session's terminal.
- You are willing to install hooks for precise waiting states and system notifications.

## Key Features

- **One board for every session**: built-in support for Claude Code, Codex, Ollama, and llama.cpp; other tools can use `ring hook`.
- **Waiting first**: sessions that need a response are sorted first and show what they need.
- **Jump back to the terminal**: supports tmux, iTerm2, Terminal.app, Neovim terminals, and Linux X11 windows.
- **Reply and name in place**: answer permission requests in supported terminals and assign recognizable session names.
- **Immediate notifications**: desktop notifications work without the board open; ntfy and webhooks can reach your phone.
- **Easy to integrate**: status-bar summaries, JSON, a provider-neutral hook, and plugin extension points.

## Quick Start

Requires Python 3.13+. The PyPI package is named `ring-cli`; the module and CLI command are named `ring`:

```sh
# Recommended: install the global command from PyPI
uv tool install 'ring-cli[tui]'

# Or use pipx
pipx install 'ring-cli[tui]'

# Print a snapshot or open the interactive TUI
ring
ring --watch
ring --watch --interval 1

# Module form also works after installation
python -m ring
```

The `[tui]` extra installs the Textual interface. Without it, `--watch` falls back to Rich polling and then plain text. In the TUI, use `↑` / `↓` to select a session, `Enter` or `Space` to return to its terminal, and `q` to quit.

For development from source, run inside the repository without installing a global command:

```sh
uv sync --all-groups
uv run ring
uv run ring --watch
```

Zero-config mode discovers local sessions automatically. For precise 🔴 waiting states, pending details, and system notifications, also run:

```sh
ring install-hooks            # merge into Claude Code and Codex hook settings
ring install-hooks --dry-run  # preview without changing settings
ring doctor                   # check hooks, notifications, and terminal focus
ring gc --dry-run             # preview stale state cleanup
```

Restart Claude Code or Codex sessions after installing hooks. `ring doctor` checks hook registration, notification backends, and terminal focus support.

## Where To Go Next

- For TUI shortcuts, permission replies, notifications, or phone push, see the [full guide](guide.md).
- To add a summary to tmux, SwiftBar, or waybar, see [output formats](guide.md#status-bar-integration-format).
- To integrate another agent CLI, terminal, or notification service, see [extensions](guide.md#extending).
- To understand how 🔴/🟢/🟡/⚫ are derived, see [session states](session-states.md).
- To work on RiNG itself, see the [contributing guide](contributing.md).

## Documentation

- [Full guide](guide.md): commands, TUI controls, terminal focus, permission replies, notifications, hooks, configuration, plugins, platforms, and privacy
- [Session states](session-states.md): how hook and zero-config modes derive 🔴/🟢/🟡/⚫, with a complete event reference
- [Contributing guide](contributing.md): development setup, tests, code style, i18n, commits, and the PR checklist

## Platform & Privacy

- **Platform**: macOS and Linux are supported; Windows is not yet supported.
- **Privacy**: by default RiNG only reads local `~/.claude/` and `~/.codex/` data and writes `~/.config/ring/`. There is no telemetry. Network access occurs only for ntfy or webhook notifiers you configure.

## Name

The name has three meanings:

1. It **rings** you when a session needs a response.
2. RiNG is the live house from *BanG Dream!*; like a venue, it lets you watch each session take the stage.
3. **R**ealtime **I**nstance **N**otification **G**rid describes the tool itself.

In the original story, RiNG is CiRCLE's second location: more bands needed another venue sharing the same backend. This tool has the same role—it does not replace agent CLIs, but adds another view over many parallel sessions.

## Non-Goals

RiNG only tracks session state and whether you need to return. It is not an agent-usage analyzer or cost dashboard. Claude Code JSONL token values are not reliable enough for accounting, so RiNG deliberately omits token and cost statistics.

## License

MIT
