# RiNG 🎤

[台灣漢語](https://github.com/Lee-W/ring/blob/main/README.md) · **English**

[![PyPI](https://img.shields.io/pypi/v/ring-cli?label=PyPI)](https://pypi.org/project/ring-cli/)
[![Python](https://img.shields.io/pypi/pyversions/ring-cli)](https://pypi.org/project/ring-cli/)
[![License](https://img.shields.io/pypi/l/ring-cli)](LICENSE)

> **R**ealtime **I**nstance **N**otification **G**rid
> — a local dashboard for active agent-CLI sessions. Claude Code and Codex are built in; other tools can plug in.

When you run several Claude Code / Codex sessions at the same time, it is easy to lose track of
which one is still working, which one has finished a turn, and which one needs your response.
RiNG puts them on one board, with sessions waiting for you sorted first.

```text
🎤 RiNG — 3 sessions on stage · 2 agent processes running

  🔴 maigo            12s  → waiting for permission
  🟢 pelican-osm       3s  → Edit
  🟡 commitizen        8m  turn finished, idle
```

## Who It Is For

- You run multiple Claude Code / Codex sessions in parallel.
- You want one board for “working”, “idle”, “waiting for me”, and “ended”.
- You want to jump from a TUI row back to the terminal where the session lives.
- You are willing to install hooks for precise waiting-state detection and system notifications.

## Key Features

- **One board for every session**: Claude Code / Codex are built in; other tools can feed `ring hook`.
- **Waiting first**: sessions that need your response are highlighted and sorted above the rest.
- **Jump back to the terminal**: in the TUI, select a session and press `Enter` / `Space` to focus tmux, iTerm2, or Terminal.app.
- **Notifies you without the board open**: with hooks installed, the moment a session turns 🔴 waiting it beeps and fires a system notification — even with no RiNG board running. With `terminal-notifier`, clicking the notification jumps back.
- **Name your sessions**: press `n` in the TUI to add a local label such as `maigo · auth refactor`.
- **Local and extensible**: RiNG only reads local Claude Code / Codex data and writes `~/.config/ring/`; session sources, focusers, and notifiers are pluggable.

## Run

Requires Python 3.13+. The PyPI package is named `ring-cli`, while the import module and CLI command are both `ring`.

```sh
# Recommended: install the command from PyPI
uv tool install 'ring-cli[tui]'

# Or use pipx
pipx install 'ring-cli[tui]'

# Then run it
ring
ring --watch
ring --watch --interval 1

# Module form also works after installation
python -m ring
```

The `[tui]` extra installs the Textual interactive UI. Without it, `--watch` falls back to Rich polling, then plain text.

For development inside the repository:

```sh
uv run ring
uv run ring --watch
```

Zero-config mode can discover local Claude Code / Codex sessions without setup. For precise 🔴 waiting detection, install hooks:

```sh
ring install-hooks            # merges into Claude Code / Codex hook settings
ring install-hooks --dry-run  # preview without writing
ring doctor                   # inspect hooks, notification backends, focusers, and config
ring gc --dry-run             # preview RiNG-owned stale state cleanup
```

Hooks only apply to new sessions, so restart Claude Code / Codex sessions after installing.

## Common Commands

| Command | Purpose |
|---------|---------|
| `ring` | Print a one-shot snapshot |
| `ring --watch` | Keep refreshing; enters the TUI when Textual is installed |
| `ring --watch --interval 1` | Refresh every second |
| `ring --watch --count N` | Stop after N frames, useful for tests / CI |
| `ring --all` | Show ended sessions too |
| `ring --no-legend` | Hide the legend |
| `ring --lang zh-Hant` | Switch UI language |
| `ring focus SESSION_ID` | Focus a specific session |
| `ring config` | Show config path and effective settings |
| `ring config set KEY VALUE` | Write one config value |
| `ring doctor` | Read-only environment diagnosis |
| `ring gc --dry-run` | Preview RiNG-owned stale state cleanup |
| `ring gc` | Clean RiNG-owned stale state files |

## Watch Mode

- With **Textual** (`[tui]` extra) in a real terminal: interactive TUI.
  Use `↑/↓` to select, `Enter` / `Space` to jump, `n` to name a session, `a` to toggle ended sessions, `r` to refresh, and `q` to quit.
  If you have vim muscle memory like I do, `j/k` move up/down and `g/G` jump to the first/last row.
- Otherwise: Rich polling; without Rich, plain text.

### Jump To A Session

Select a session and press `Enter`. RiNG focuses the terminal where that session is running.

- **tmux**: switches directly to the pane via `switch-client`.
- **iTerm2 / Terminal.app** on macOS: uses the session `tty` and AppleScript to focus the matching tab. The first run may ask for macOS Automation permission.

TTY matching is most accurate in hook mode. Without hooks, Codex falls back to zero-config matching:
one live Codex session per cwd can jump correctly; multiple live Codex sessions in the same cwd are shown conservatively to avoid focusing the wrong tab.

### Notifications

Default behavior: with hooks installed, the moment a session changes to 🔴 waiting, the hook beeps
and sends a system notification right then — **no RiNG board needs to be open**, so it rings you even
after you close the terminal (notifications are event-driven, not polled). While a TUI is open, it
also nudges you again in-app at 30s / 120s / 300s if the session keeps waiting.

For clickable notifications on macOS, install `terminal-notifier`:

```sh
brew install terminal-notifier
```

Without it, RiNG falls back to macOS text notifications without click-to-focus.
Notification sound, repeat timing, and backend selection are configurable; notification backends
are also pluggable via the `Notifier` extension point.

### Cleaning RiNG State Files (`ring gc`)

When RiNG receives `SessionEnd`, it removes its own hook registry entry. If an agent crashes or the
final hook does not run, ended `~/.config/ring/sessions/*.json` files can remain. They are hidden from
the board by default, and you can remove them with `ring gc`.

```sh
ring gc --dry-run        # preview what would be deleted
ring gc                  # delete ended registry files older than 7 days, plus expired IPC files
ring gc --older-than 1d  # use a 1-day threshold
ring gc --all-ended      # delete every registry file currently classified as ended
```

`ring gc` only cleans state files RiNG owns under `~/.config/ring/`. It does not touch Claude Code /
Codex transcripts or state. `ring doctor` remains read-only and never deletes files.

## Session Sources

RiNG collects sessions from registered sources. Built-ins:

| Source | Reads From | Precision |
|--------|------------|-----------|
| **Claude Code zero-config** | `~/.claude/projects/**/*.jsonl`, mtimes, and `cwd` fields | no setup; detects recent activity and turn completion. Precise user-action prompts require hooks |
| **Codex zero-config** | `~/.codex/state_5.sqlite`, rollout JSONL, and live `codex` processes | no setup; detects live / ended / turn completion. Use hooks for precise jumps when multiple sessions share a cwd |
| **hook registry** | `~/.config/ring/sessions/`, written by `ring hook` | precise: 🔴 waiting / 🟢 working / 🟡 idle / ⚫ ended |

Zero-config needs no setup. For precise “who needs me”, install hooks so provider events feed the RiNG registry.
RiNG includes installers for Claude Code and Codex; other tools can use the provider-neutral `ring hook` protocol.

## States

RiNG reduces every session to four user-facing states. 🔴 waiting is sorted first.

| State | Meaning | What RiNG Saw |
|-------|---------|---------------|
| 🔴 waiting | You should return now | hook event requiring a user response, such as permission or choices |
| 🟢 working | Agent is running | prompt submitted or recent activity |
| 🟡 idle | The current turn finished | `Stop`, or no new activity past `working_threshold_seconds` |
| ⚫ ended | Session is over | `SessionEnd`, process ended, or local records aged out |

🔴 waiting requires hook mode. Zero-config can tell whether a session was recently active, but not whether it needs a decision from you.

## Hook Mode

Zero-config only has filesystem mtimes and local state snapshots, so it cannot reliably distinguish “needs a decision” from “just finished”.
Hooks send agent-CLI events directly to RiNG, making 🔴 waiting and notifications precise.

### Claude Code / Codex Installer

Hooks run `ring hook`, so install `ring` somewhere stable on `PATH` first:

```sh
uv tool install 'ring-cli[tui]'
```

Then register hooks:

```sh
ring install-hooks
ring install-hooks --dry-run
```

Claude Code hooks are written to `~/.claude/settings.json`. If `~/.codex` exists, Codex hooks are also written to
`~/.codex/hooks.json`; Codex will ask you to trust the hook before it runs it.

Claude Code events:

| Claude Code Event | RiNG State |
|-------------------|------------|
| `SessionStart` / `UserPromptSubmit` | 🟢 working |
| `Stop` | 🟡 idle |
| `Notification` with `permission_prompt` / `elicitation_dialog` | 🔴 waiting |
| `PermissionRequest` / `PreToolUse` with `AskUserQuestion` | 🔴 waiting |
| `SessionEnd` | removed from the board |

Codex currently installs the supported interactive events: `PreToolUse`, `PermissionRequest`, and `Stop`.

Verify that hooks are writing:

```sh
ls ~/.config/ring/sessions/
```

### Other Providers

Any tool can feed JSON into `ring hook`:

```sh
ring hook --provider codex
# shorthand:
ring hook codex
```

Payload field names are intentionally loose. At minimum, provide a session id, event, and cwd:

```json
{
  "provider": "codex",
  "session_id": "thread-123",
  "event": "Notification",
  "notification_type": "permission_prompt",
  "requires_action": true,
  "waiting_for": "permission",
  "cwd": "/repo/app",
  "tty": "/dev/ttys003",
  "last_action": "waiting for permission"
}
```

If a provider can distinguish “needs user action now” from “just waiting for the next prompt”,
send `requires_action = true/false` or `waiting_for = "permission" | "options" | "next_step"`.

### agent-hooks

Notification details: system notifications are sent by `ring hook` at the event (both `--watch` and
the TUI only render the board; they no longer send system notifications). The default backend is `auto`:
RiNG prefers clickable `terminal-notifier`, then falls back to `osascript` on macOS or `notify-send`
on Linux. If none are available, RiNG keeps the board running and skips notifications.

Config keys include `notify_sound`, `notify_sound_name`, `notify_ignore_dnd`,
`notify_repeat_seconds`, `notify_repeat_max`, and `notify_backend`. Custom notification channels
can be added by registering another `Notifier`.

`agent-hooks` is an optional external hook helper / decision UI. If it is installed and
`notify_backend = "agent-hooks"`, `ring hook` still writes RiNG registry state, then passes the
raw payload to `agent-hooks callback` for the synchronous decision UI. RiNG `--watch` will not send
a duplicate notification. If `agent-hooks` is not on `PATH`, RiNG automatically falls back to `auto`.

### Remove Hooks

```sh
ring remove-hooks
ring remove-hooks --dry-run
```

This removes RiNG-installed hook entries from Claude Code / Codex settings. It does not delete
`~/.config/ring/` session records and does not touch other hooks.

## Configuration

`~/.config/ring/config.toml`, all optional:

```toml
lang = "en"
interval = 2.0
show_all = false
legend = true
active_window_seconds = 21600
working_threshold_seconds = 90
waiting_window_seconds = 1800
notify_sound = true
notify_sound_name = "Glass"
notify_ignore_dnd = false
notify_backend = "auto"          # auto / terminal-notifier / osascript / notify-send / agent-hooks / none
notify_repeat_seconds = [30, 120, 300]
notify_repeat_max = 3
focusers = ["tmux", "iTerm2", "Terminal"]

[colors]
waiting = "bold red"
working = "green"
idle = "yellow"
ended = "grey50"
project = "cyan"
location = "bright_blue"
muted = "grey50"
```

## Extending

RiNG is not tied to a specific tool or terminal.

| Extension Point | Purpose | Built-ins |
|-----------------|---------|-----------|
| `SessionSource` | find sessions | Claude Code, Codex, hook registry |
| `Focuser` | jump to terminals | tmux, iTerm2, Terminal.app |
| `Notifier` | notify when sessions are waiting | terminal-notifier, osascript, notify-send |

Each backend is a small module under `ring/sources/`, `ring/focus/`, or `ring/notify/`, registered via `register_*()`.

## Platform & Privacy

- **Platform**: macOS / Linux. Windows is not supported.
- **Privacy**: entirely local. RiNG only reads local `~/.claude/` and `~/.codex/` data and writes
  `~/.config/ring/`. No network, uploads, or telemetry.

## Name

The name is a triple pun:

1. It **rings** you when a session needs a response.
2. “RiNG” is the live house from *BanG Dream!*.
3. **R**ealtime **I**nstance **N**otification **G**rid describes what it is.

## Non-Goals

RiNG tracks session state. It is not a token or cost dashboard.

Claude Code JSONL token counts are currently unreliable enough to make cost accounting misleading,
so RiNG deliberately avoids that surface.

## License

MIT
