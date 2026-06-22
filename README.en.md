# RiNG 🎤

[台灣漢語](README.md) · **English**

> **R**ealtime **I**nstance **N**otification **G**rid
> — a stage where all your active agent-CLI sessions perform (Claude Code / Codex built in, extensible).

You've got several Claude Code / Codex sessions running at once and can't tell which one
is waiting for your reply, which is still working, and which died long ago. RiNG puts them all
on one stage at a glance — **whoever's waiting for you comes first**. When a session needs you,
it literally **rings** you.

The name is a triple pun: 📞 it **rings** you (a waiting-for-reply ping) + 🎤 the live house
"RiNG" from *BanG Dream! It's MyGO!!!!!* (a venue — you sit and watch one band, i.e. one session,
after another) + **R**ealtime **I**nstance **N**otification **G**rid (what it actually is).

## Run

`ring` has to be installed as a command first — cloning and typing `ring` gives `command not found`.

```sh
# During development: run straight from the repo (uv builds the entry point)
uv run ring                       # one-shot snapshot
uv run ring --watch               # keep refreshing, Ctrl-C to leave
uv run ring --watch --interval 1  # custom refresh seconds

# Install as a global command, then just type ring
uv tool install '.[tui]'          # [tui] brings the Textual UI; or pipx install '.[tui]'
ring --watch

# Once installed, the module form works too
python -m ring
```

`--count N` makes `--watch` stop after N frames (for CI / tests).

### Two faces of `--watch`

- With **Textual** (`[tui]` extra) in a real terminal → an **interactive TUI**:
  `↑/↓` select a session, `Enter`/`Space` jump to its terminal, `a` toggle ended ones, `r` refresh, `q` quit.
- Otherwise → **Rich poll** (clear and redraw); without even Rich, plain text. Three tiers of graceful degradation.

### Jump to a session (`Enter` / `Space`)

Pick a session and press `Enter` — RiNG brings focus to the terminal it actually lives in. The
terminal integration is a **pluggable focuser**; the core isn't tied to any vendor:

- **tmux**: `switch-client` straight to that pane (you and it must share a tmux server).
- **iTerm2 / Terminal.app** (macOS): use the session's `tty` to focus the matching tab via
  AppleScript, picking the right app automatically (an app that isn't running won't be woken up).
  The first time you'll get a system "Automation" prompt — allow once.

Which session lives in which terminal is matched by its `tty` — **most precise in Claude Code
hook mode**; under zero-config it also lines up when a project has only one session. Codex
currently uses zero-config: jump works when a cwd has one live Codex process; with multiple
live Codex sessions in the same cwd, RiNG shows them conservatively to avoid focusing the wrong tab.

### It rings you 🔔

With hooks installed, when a session flips from working to 🔴 waiting, RiNG **beeps and notifies** —
true to its name, it really rings you. If it stays waiting, RiNG can remind you again on a
configurable schedule. (Zero-config can't detect WAITING, so this needs hook mode.)

## Two data sources

| Mode | Source | Status precision |
|------|--------|------------------|
| **Claude Code zero-config** (default) | scans `~/.claude/projects/**/*.jsonl` mtimes + the `cwd` field in records | medium (can infer turn-ended from transcript tail; can't see permission notifications) |
| **Codex zero-config** (default) | reads `~/.codex/state_5.sqlite` threads + rollout JSONL and matches live `codex` processes to ttys | medium (live / ended / turn-ended; same-cwd multi-session jumps are not guaranteed) |
| **hook registry** (opt-in, precise) | RiNG hooks write `~/.config/ring/sessions/` on `Notification` / `UserPromptSubmit` / `Stop` / `SessionEnd` | precise (🔴 waiting / 🟢 working / 🟡 idle / ⚫ ended) |

Zero-config needs no setup; for precise "who's waiting for me", feed provider hook events into
RiNG's registry. RiNG ships a Claude Code installer; Codex and future tools can use the
provider-neutral `ring hook` protocol directly.

## hook mode (precise "waiting for reply")

Zero-config only has file mtimes or local state snapshots, so it **can't reliably tell "waiting
for your reply" from "just finished"**. With hooks, RiNG receives agent-CLI events directly and
the status becomes precise — 🔴 waiting plus the beep both rely on it.

### Claude Code: built-in installer

1. **Install globally** so `ring` is on `PATH` (Claude hooks run `ring hook`):

   ```sh
   uv tool install '.[tui]'
   ```

2. **Register the hooks:**

   ```sh
   ring install-hooks            # writes ~/.claude/settings.json (merges, doesn't clobber)
   ring install-hooks --dry-run  # preview without touching the file
   ```

   | Claude Code event | RiNG status |
   |---|---|
   | `SessionStart` / `UserPromptSubmit` | 🟢 working |
   | `Stop` / `Notification` | 🔴 waiting |
   | `SessionEnd` | disappears from the board |

3. **Reopen sessions** (hooks only apply to new ones) and verify:

   ```sh
   ls ~/.config/ring/sessions/   # a <session_id>.json means hooks are writing
   ```

### Codex / Future Providers: Neutral Hook Protocol

RiNG does not depend on agent-hooks. Any tool can feed JSON into `ring hook` when an event happens:

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
  "event": "Stop",
  "cwd": "/repo/app",
  "tty": "/dev/ttys003",
  "last_action": "finished responding"
}
```

Event semantics match Claude Code: `SessionStart` / `UserPromptSubmit` → 🟢, `Stop` /
`Notification` → 🔴, and `SessionEnd` removes the session from the board. Non-Claude session ids
are provider-qualified automatically, for example `codex:thread-123`, to avoid collisions.

**System notifications (🔔 click-to-focus):** when a session turns 🔴 waiting, RiNG sends a
system notification (both headless `--watch` and TUI). If it stays waiting, RiNG reminds you
again according to `notify_repeat_seconds` (30s / 120s / 300s by default). Clicking the
notification jumps back to the RiNG TUI and selects that session; if no TUI is running, it jumps
straight to the session terminal. Install `terminal-notifier` for click-to-focus and notification
sound; without it, notifications fall back to macOS alerts (sound works, no click action), and
RiNG will show a one-time install hint the first time:

```sh
brew install terminal-notifier
```

To remove hooks:

```sh
ring remove-hooks             # removes ring hook entries from ~/.claude/settings.json
ring remove-hooks --dry-run   # preview without touching the file
```

## Configuration (optional)

`~/.config/ring/config.toml`, everything optional, missing keys fall back to defaults:

```toml
lang = "zh-Hant"                 # default language (CLI --lang wins, then RING_LANG / LANG)
interval = 2.0                   # watch refresh seconds
show_all = false                 # show ended sessions by default?
legend = true                    # show the color legend by default?
active_window_seconds = 21600    # only look at sessions touched recently (6h)
working_threshold_seconds = 90   # idle this long → 🟢 working becomes 🟡 idle
notify_sound = true              # play a sound for system notifications
notify_sound_name = "Glass"      # macOS / terminal-notifier sound name
notify_repeat_seconds = [30, 120, 300]  # remind again if a session keeps waiting
notify_repeat_max = 3            # max repeat reminders; 0 = unlimited
focusers = ["tmux", "iTerm2", "Terminal"]   # jump attempt order

[colors]                         # Rich style strings, override per item
waiting = "bold red"
working = "green"
idle = "yellow"
ended = "grey50"
project = "cyan"
location = "bright_blue"
muted = "grey50"
```

## Why no token / cost stats

Claude Code's JSONL token counts are broken (input off by ~100×, output by ~10×), and every tool
that bills off them is affected. RiNG only shows **status**, no cost accounting — deliberately
sidestepping that landmine.

## Language / translation

The UI uses **gettext**, with msgids written in **Taiwanese Mandarin** — so the default (zh-Hant)
needs no `.mo` at all, the source is the translation. Switch with `--lang en`. Plurals are handled
properly via `ngettext` (`1 session` vs `2 sessions`).

To add a language or after changing strings:

```sh
poe i18n-extract     # extract msgids from source → src/ring/locale/ring.pot
# copy ring.pot to src/ring/locale/<lang>/LC_MESSAGES/ring.po, fill in msgstr
poe i18n-compile     # .po → .mo (commit the .mo; the wheel carries it automatically)
```

## Extending

The core is tied to no specific tool or terminal. Both extension points are "write a small class
and register it", with zero changes to the main flow.

### Another agent CLI (`SessionSource`)

`HookRegistrySource` is built in (reads `~/.config/ring/sessions/`), as are `ClaudeCodeSource`
(scans `~/.claude`) and `CodexSource` (reads `~/.codex/state_5.sqlite`). Prefer feeding
`ring hook` when the tool has hooks; if not, write a source that emits `Session` objects and
register it:

```python
from ring.registry import Session, Status
from ring.sources import register_source


class MyToolSource:
    name = "mytool"

    def discover(self) -> list[Session]:
        return [Session(session_id="…", cwd="…", status=Status.WORKING,
                        last_active=0.0, last_action="→ …", source="mytool")]


register_source(MyToolSource())
```

`Session` is tool-neutral (id / cwd / status / last_action / tty / todo…); each source decides how
to fill it.

### Another terminal (`Focuser`)

Jump integration works the same way — write a class matching the `Focuser` protocol
(`try_focus(session) -> (ok, msg) | None`) and call `ring.focus.register_focuser(MyFocuser())`.
tmux / iTerm2 / Terminal.app ship built in, each as its own module (`ring/focus/tmux.py`, …).

## Platform & privacy

- **Platform**: macOS / Linux (detection via `ps` / `lsof` / `tmux`; Windows unsupported).
- **Privacy**: entirely local — it only **reads** local `~/.claude/` and `~/.codex/` data, and
  only **writes** `~/.config/ring/`. No network, no uploads, nothing sent anywhere.

## License

MIT
