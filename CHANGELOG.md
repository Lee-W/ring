## 0.9.0 (2026-07-10)

### BREAKING CHANGE

- the idle_threshold_seconds config option is removed

### Feat

- remove idle-too-long notifications

### Fix

- session liveness and waiting-state misdetection

## 0.8.1 (2026-07-08)

### Fix

- "ring --version" not showing

## 0.8.0 (2026-07-08)

### Feat

- notify when a session sits idle too long
- add ring digest command
- monitor hook heartbeat freshness
- classify waiting sessions
- polish ring focus command
- improve session cleanup and targeting

### Fix

- refuse hiding synthetic process rows

## 0.7.0 (2026-07-03)

### Feat

- focus Neovim terminal sessions

### Fix

- skip dead terminal buffers when focusing Neovim sessions

## 0.6.0 (2026-07-03)

### Feat

- add --format output, remote notify, plugins, stats & completion

## 0.5.0 (2026-06-28)

### Feat

- jump back to Linux terminals via wmctrl

### Refactor

- split transcript parsing out of registry

## 0.4.0 (2026-06-27)

### Feat

- ring you from the hook the moment a session needs you

### Fix

- keep stale live codex thread on symlinked cwd

## 0.3.0 (2026-06-25)

### Feat

- add ring gc

### Refactor

- split cli command handlers

## 0.2.0 (2026-06-25)

### Feat

- add notify_ignore_dnd to bypass Do Not Disturb on macOS
- add `ring doctor` read-only environment diagnostics
- add vim-style j/k/g/G navigation to the TUI
- add `ring config` to show, get, and set settings
- install/remove RiNG hooks for Codex (~/.codex/hooks.json) too
- delegate decisions to agent-hooks via notify_backend = "agent-hooks"
- support notify_backend = "none" to run RiNG as a silent board
- add notify_backend config to force osascript notifications
- hide the tool column when every session is the same tool
- name a session in the TUI to mark what it is
- show the tool (Claude / Codex) in a dedicated column
- warn when other tools hook the same permission events
- support explicit action-required hook state
- add provider-neutral hook protocol
- make waiting notifications harder to miss
- add codex session support
- 通知可點擊跳回 RiNG TUI 並選中等你回話的 session
- notify on waiting sessions
- **sources**: 替有 live process 卻無 transcript 的 session 補列
- **registry**: scan 模式偵測 🔴 待回覆狀態
- **cli**: 路徑過長時中段省略，避免擠掉動作欄
- RiNG 0.1.0 — 看所有 active Claude Code session 的場館 TUI

### Fix

- ignore Claude background processes
- **tui**: re-evaluate tool column on every refresh
- warn that Codex must trust the hook, and self-heal stale timeouts
- warn that Codex must trust the hook, and self-heal stale timeouts
- make notification body the location, not a cryptic repeated name
- purge un-jumpable phantom rows left by the source-as-provider bug
- stop hiding a lone live session when its recorded tty is wrong
- match sessions to live processes through symlinked paths
- stop reading SessionStart "source" as the provider
- clear waiting state on tool activity and keep notification routing alive
- clear stale waiting state from newer scans
- install action-required claude hooks
- narrow waiting state to action-required events
- hide stale hook sessions
- **registry**: scan 模式改用開場 cwd 歸屬 session，修中途 cd 漂到別專案

### Refactor

- split notify and sources into pluggable packages
- make notifications a pluggable Notifier abstraction
- make provider liveness detection registry-based, not hardcoded
- 抽象出 SessionSource、focus 拆成 package、dist 改名 ring
