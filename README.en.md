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
- **Jump back to the terminal**: in the TUI, select a session and press `Enter` / `Space` to focus tmux, iTerm2, Terminal.app (macOS), or a Linux X11 window (`wmctrl`).
- **Notifies you without the board open**: with hooks installed, the moment a session turns 🔴 waiting it beeps and fires a system notification — even with no RiNG board running. With `terminal-notifier`, clicking the notification jumps back.
- **Reply to permission requests in place**: with the cursor on a 🔴 waiting row, press `p` — RiNG reads the permission dialog from that session's terminal screen, pops the numbered options in a modal, and presses your choice for you, so you don't have to jump over one by one (sessions inside tmux, plus plain iTerm2 tabs on macOS).
- **Name your sessions**: press `n` in the TUI to name a session, e.g. "auth refactor"; once named, the board and notifications show the name instead of the workspace directory.
- **See what it is waiting for**: in hook mode, a 🔴 waiting session carries the concrete pending item (the command to run, the question asked) — shown in the TUI and in the notification body.
- **Fits your status bar**: `ring --format oneline` prints a `🔴2 🟢1 🟡3` one-liner for tmux / SwiftBar / waybar; `--format json` feeds scripts.
- **Rings your phone too**: built-in ntfy / webhook notifiers push 🔴 waiting to your phone when you are away from the desk.
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
| `ring focus SESSION_ID` | Focus a specific session; unique prefixes work |
| `ring config` | Show config path and effective settings |
| `ring config set KEY VALUE` | Write one config value |
| `ring doctor` | Read-only environment diagnosis |
| `ring digest --since 4h` | Away summary: recent session state and wait stats |
| `ring gc --dry-run` | Preview RiNG-owned stale state cleanup |
| `ring gc` | Clean RiNG-owned stale state files |
| `ring --format json` | Machine-readable board snapshot (for jq / scripts) |
| `ring --format oneline` | `🔴2 🟢1 🟡3` one-liner (for status bars) |
| `ring stats` | Waiting stats: how long agents kept 🔴 waiting in the last 7 days |
| `ring completion zsh` | Print a shell completion script (zsh / bash) |

### Status Bar Integration (`--format`)

```sh
ring --format oneline        # 🔴2 🟢1 🟡3 (empty output when no sessions, so the segment collapses)
ring --format json | jq '.counts.waiting'
```

- **tmux**: `set -g status-right '#(ring --format oneline) …'` (with `status-interval 5`).
- **SwiftBar / xbar / waybar**: wrap `ring --format oneline` or consume the JSON.
- JSON keys are a stable interface (additive only), safe to script against.

### Shell Completion (`ring completion`)

```sh
# ~/.zshrc
eval "$(ring completion zsh)"
# ~/.bashrc
eval "$(ring completion bash)"
```

Completes subcommands, flags, and `config set` keys; `ring focus` prompts for a session id or unique prefix.

## Watch Mode

- With **Textual** (`[tui]` extra) in a real terminal: interactive TUI.
  Use `↑/↓` to select, `Enter` / `Space` to jump, `p` to reply to a permission request in place, `n` to name a session, `a` to toggle ended sessions, `dd` to hide a session (it reappears automatically once it has new activity), `r` to refresh, and `q` to quit.
  If you have vim muscle memory like I do, `j/k` move up/down and `g/G` jump to the first/last row.
  When the selected row is 🔴 waiting, a line under the table shows **what it is concretely waiting for** (the command to run, the question asked; hook mode only).
  Claude Code background agents carry a `⚙` badge. They have no terminal to jump to, so selecting one shows a `claude --resume` hint; completed agents are folded into ended sessions by default and remain available via `a`.
- Otherwise: Rich polling; without Rich, plain text.

### Jump To A Session

Select a session and press `Enter`, or run `ring focus SESSION_ID` with a full id or unique prefix.
RiNG focuses the terminal where that session is running. If the TUI is already open, `ring focus`
hands the request to the TUI so it selects that session; otherwise it focuses the terminal directly.

- **tmux**: switches directly to the pane via `switch-client`.
- **Neovim `:terminal`**: uses the terminal job's inherited `$NVIM` server socket to switch to the exact buffer, then lets the outer tmux or terminal focuser raise its pane/window.
- **iTerm2 / Terminal.app** on macOS: uses the session `tty` and AppleScript to focus the matching tab. The first run may ask for macOS Automation permission.
- **Linux X11 window** (`wmctrl`, best-effort fallback): for Linux without tmux — walks from the `tty` up to the terminal window that owns it and raises it via `wmctrl`. **Limits**: X11 only (usually a no-op on Wayland), raises the whole window but cannot pick the tab, and gnome-terminal's client/server model may not match. Requires `apt install wmctrl`.

TTY matching is most accurate in hook mode. Without hooks, Codex falls back to zero-config matching:
one live Codex session per cwd can jump correctly; multiple live Codex sessions in the same cwd are shown conservatively to avoid focusing the wrong tab.

### Reply To Permission Requests In Place (`p`)

With the cursor on a 🔴 waiting row, press `p`. RiNG reads that session's screen, parses the
permission dialog (Claude Code's "Do you want to proceed?" box, including background-subagent
ones with a "from the … agent" header), and lists the numbered options verbatim in a modal.
After you pick one, RiNG **captures the screen again** to confirm the dialog is still there and
unchanged (it may have been answered while you were deciding), only then sends that single
digit, and re-checks that the dialog actually disappeared.
Once the reply is verified, the TUI immediately clears that waiting revision. Only a newer hook
event, such as another permission request, can mark the session as waiting again.

- **Sessions inside tmux**: reads the screen with `tmux capture-pane` and sends keys with
  `tmux send-keys`.
- **Sessions in a plain iTerm2 tab on macOS** (no tmux): locates the matching iTerm2 session by
  its `tty` via AppleScript, and both reads the screen and sends keys through `osascript`. The
  first use triggers the macOS Automation permission prompt ("allow control of iTerm2") — allow
  it once.

Safety first: if no recognizable dialog can be parsed (missing markers, numbering, or cursor),
RiNG only shows a toast and **never sends a key** — without the dialog, keystrokes would land in
the chat input box as text. If the dialog happens to vanish in the instant the digit is sent and
the digit lands in the input box, RiNG sends a Backspace to clean it up and warns you.

**Limits**: reading the screen requires a tmux pane coordinate, or (on macOS) an iTerm2 session
with a detectable tty; for other sessions, press `Enter` to jump over and reply there.

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

#### Push To Your Phone (ntfy / webhook)

Desktop notifications do not help when you are away from the desk. Point RiNG at an
[ntfy](https://ntfy.sh) topic to push to your phone:

```toml
# ~/.config/ring/config.toml
notify_ntfy_url = "https://ntfy.sh/my-ring-topic"  # subscribe to the same topic in the ntfy app
notify_also = ["ntfy"]                             # desktop notification as usual, plus a copy to the phone
```

`notify_backend = "ntfy"` pushes to the phone only. For Slack / your own bot / IFTTT, set
`notify_webhook_url` to use the generic webhook backend (JSON POST with a stable, additive-only payload).

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

### Waiting Stats (`ring stats`)

In hook mode, RiNG logs session **state transitions** to `~/.config/ring/events.jsonl`
(transitions only, tiny, self-trimming past a size cap). `ring stats` then tells you, per project,
how many times an agent 🔴 waited on you and for how long (avg / max / total).

```sh
ring stats               # last 7 days
ring stats --since 12h   # custom window
```

Like precise notifications, 🔴 waiting is invisible to zero-config, so stats also needs hook mode.

## Session Sources

RiNG collects sessions from registered sources. Built-ins:

| Source | Reads From | Precision |
|--------|------------|-----------|
| **Claude Code zero-config** | `~/.claude/projects/**/*.jsonl`, mtimes, and `cwd` fields | no setup; detects recent activity and turn completion. Precise user-action prompts require hooks |
| **Codex zero-config** | `~/.codex/state_5.sqlite`, rollout JSONL, and live `codex` processes | no setup; detects live / ended / turn completion. Use hooks for precise jumps when multiple sessions share a cwd |
| **Ollama zero-config** | interactive `ollama run` processes with a controlling terminal | process-liveness only; shows cwd, TTY, and model, and excludes `ollama serve` |
| **llama.cpp zero-config** | interactive `llama-cli` processes with a controlling terminal | process-liveness only; shows cwd, TTY, and model, and excludes `llama-server` |
| **hook registry** | `~/.config/ring/sessions/`, written by `ring hook` | precise: 🔴 waiting / 🟢 working / 🟡 idle / ⚫ ended |

Zero-config needs no setup. For precise “who needs me”, install hooks so provider events feed the RiNG registry.
RiNG includes installers for Claude Code and Codex; other tools can use the provider-neutral `ring hook` protocol.

Ollama and llama.cpp do not expose a session transcript or interaction hooks that RiNG can read, so their
zero-config rows stay 🟡: the row means the interactive CLI is alive, not that RiNG can distinguish generation
from waiting for the next prompt. The row disappears when the CLI exits. An outer agent that can emit lifecycle
events can use the provider-neutral hook protocol for precise states.

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

`Stop` has one more exception: an agent sometimes doesn't use `AskUserQuestion` or trigger a permission
request, and just asks a plain-text question before stopping (e.g. "want me to fix B too?"). RiNG looks at
the **end** of the last assistant message for this: if it ends in a question mark (`？` / `?`, with any
trailing fenced code block stripped first) the session is promoted to 🔴 waiting (`waiting_kind="question"`),
with the question as its detail; a question that only appears mid-message, with a statement at the end,
does not count — conservative on purpose, biased toward missing a case rather than a false positive. This
applies to both Claude Code and Codex (`Stop` payloads from both carry `last_assistant_message`); the
`detect_stop_questions` config key can turn it off (on by default).

Codex currently installs the supported interactive events: `PreToolUse`, `PermissionRequest`, `PostToolUse`,
and `Stop`. Codex also emits `PermissionRequest` before an existing policy auto-approves the call, so a bare
event stays 🟢 working at the hook level. Codex hooks have no "user approved" event and no heartbeat — while
an approval prompt is pending, the hook channel is **completely silent**. RiNG therefore treats the silence
itself as the signal: when the last hook event is a `PermissionRequest` and nothing has followed for more than
`codex_permission_wait_seconds` (default 10s), the board marks the session 🔴 waiting with the pending command
as its detail; any subsequent event (the next tool call, `Stop`) naturally clears it.

Known Codex-side limitations (verified against 0.144.4; the hook payload has no field that could distinguish
these):

- **Approve and deny are indistinguishable**: neither emits a hook event. After you approve a long-running
  command, 🔴 lingers until the command finishes (`PostToolUse`); after a deny with no immediate follow-up
  action, 🔴 lingers until the next event or `Stop`.
- The system notification for this timeout-promoted 🔴 is sent by the TUI's alert scheduler (there is no hook
  event to notify from); headless `--watch` does not send this particular notification.

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
`notify_repeat_seconds`, `notify_repeat_max`, `notify_backend`, and `waiting_cooldown_seconds`.
Custom notification channels can be added by registering another `Notifier`.

Background subagent permission requests can make a session flap between waiting and working in
quick succession. `waiting_cooldown_seconds` (180s by default) stops `ring hook`'s system
notification and the TUI's bell/reminder from firing again the instant a session re-enters waiting
within the cooldown window of its last alert; set it to `0` to disable the cooldown (alert on
every re-entry, the old behavior).

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
codex_permission_wait_seconds = 10  # bare Codex PermissionRequest + hook silence beyond this → 🔴 waiting; 0 = off
detect_stop_questions = true     # promote Stop to 🔴 waiting when it ends in a plain-text question; false = Stop always 🟡
notify_sound = true
notify_sound_name = "Glass"
notify_ignore_dnd = false
notify_backend = "auto"          # auto / terminal-notifier / osascript / notify-send / agent-hooks / none
notify_repeat_seconds = [30, 120, 300]
notify_repeat_max = 3
waiting_cooldown_seconds = 180   # suppress an immediate re-alert on re-entering waiting within this window; 0 = off
notify_ntfy_url = ""             # full ntfy topic URL enables phone push (e.g. https://ntfy.sh/my-topic)
notify_webhook_url = ""          # URL enables the generic webhook backend (JSON POST)
notify_also = []                 # extra backends fired besides the primary, e.g. ["ntfy"]
focusers = ["Neovim", "tmux", "iTerm2", "Terminal", "linux-wm"]
plugins = []                     # external plugin modules imported at startup (see Extending)

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
| `SessionSource` | find sessions | Claude Code, Codex, Ollama, llama.cpp, hook registry |
| `Focuser` | jump to terminals | tmux, iTerm2, Terminal.app, Linux X11 (wmctrl) |
| `Notifier` | notify when sessions are waiting | terminal-notifier, osascript, notify-send, ntfy, webhook |

Each backend is a small module under `ring/sources/`, `ring/focus/`, or `ring/notify/`, registered via `register_*()`.

### Loading Your Plugin Into An Installed `ring`

`register_*()` only counts if something runs it. The installed `ring` command loads plugins from
two places at startup:

1. **Entry point** (for published packages) — declare it in your package's `pyproject.toml`,
   pointing at a module or callable (a module registers on import; a callable is invoked once
   with no arguments):

   ```toml
   [project.entry-points."ring.plugins"]
   mytool = "ring_mytool.plugin"
   ```

2. **Config** (for local scripts) — add `plugins = ["my_module"]` to `~/.config/ring/config.toml`;
   the module must be importable (site-packages or `PYTHONPATH`).

A broken plugin prints one warning line to stderr and never blocks the board.

## Platform & Privacy

- **Platform**: macOS / Linux. Windows is not supported.
- **Privacy**: local by default. RiNG only reads local `~/.claude/` and `~/.codex/` data and writes
  `~/.config/ring/`. No telemetry. The only network calls are the optional ntfy / webhook notifiers,
  and only to URLs you configure yourself.

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
