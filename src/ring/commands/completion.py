"""``ring completion SHELL``——印出 zsh / bash 補全腳本。

CLI 是手寫 dispatch（不是純 argparse subparsers），所以補全腳本也自己生：
子命令 / 旗標寫死在模板，config 鍵在生成時從 ``settable_keys()`` 動態帶入，
新增設定鍵不必回來改這裡。用法::

    # zsh（放 ~/.zshrc）
    eval "$(ring completion zsh)"

    # bash（放 ~/.bashrc）
    eval "$(ring completion bash)"
"""

from __future__ import annotations

import sys

from ring.commands._args import strip_lang as _strip_lang
from ring.config import settable_keys
from ring.i18n import gettext as _

# 子命令與頂層旗標——跟 cli.main 的 dispatch / argparse 保持同步。
_COMMANDS = (
    "hook",
    "install-hooks",
    "remove-hooks",
    "config",
    "focus",
    "gc",
    "doctor",
    "digest",
    "stats",
    "completion",
)
_TOP_FLAGS = (
    "--watch",
    "--interval",
    "--count",
    "--all",
    "--legend",
    "--no-legend",
    "--lang",
    "--format",
    "--version",
    "--help",
)

_ZSH_TEMPLATE = """\
# ring zsh completion — eval "$(ring completion zsh)"
_ring() {{
  local -a commands
  commands=(
    'hook:read a provider hook payload from stdin'
    'install-hooks:install Claude Code / Codex hooks'
    'remove-hooks:remove Claude Code / Codex hooks'
    'config:show / get / set configuration'
    'focus:focus a session by id'
    'gc:clean stale RiNG state files'
    'doctor:read-only environment diagnosis'
    'digest:away summary'
    'stats:waiting-time statistics'
    'completion:print shell completion script'
  )

  if (( CURRENT == 2 )); then
    _describe -t commands 'ring command' commands
    _arguments \\
      '--watch[keep refreshing]' \\
      '--interval[refresh seconds]:seconds:' \\
      '--count[frames before exit]:count:' \\
      '--all[include ended sessions]' \\
      '--legend[show legend]' \\
      '--no-legend[hide legend]' \\
      '--lang[UI language]:lang:(en zh-Hant)' \\
      '--format[output format]:format:(table json oneline)' \\
      '--version[show version]'
    return
  fi

  case $words[2] in
    focus)
      _message 'session id or unique prefix'
      ;;
    hook)
      _arguments '--provider[provider name]:provider:(claude-code codex)'
      ;;
    install-hooks|remove-hooks)
      _arguments '--dry-run[preview only, no writes]'
      ;;
    gc)
      _arguments \\
        '--dry-run[preview only, no deletes]' \\
        '--older-than[age threshold, e.g. 30m / 2h / 7d]:duration:' \\
        '--all-ended[remove every ended registry]'
      ;;
    digest)
      _arguments \\
        '--since[summary window, e.g. 30m / 4h / 1d]:duration:' \\
        '--format[output format]:format:(text json)'
      ;;
    completion)
      (( CURRENT == 3 )) && _values 'shell' zsh bash
      ;;
    config)
      if (( CURRENT == 3 )); then
        _values 'config action' get set
      elif (( CURRENT == 4 )); then
        _values 'config key' {keys}
      fi
      ;;
  esac
}}
if ! command -v compdef >/dev/null 2>&1; then
  autoload -Uz compinit
  compinit
fi
compdef _ring ring
"""

_BASH_TEMPLATE = """\
# ring bash completion — eval "$(ring completion bash)"
_ring_completion() {{
  local cur prev
  cur="${{COMP_WORDS[COMP_CWORD]}}"
  prev="${{COMP_WORDS[COMP_CWORD - 1]}}"

  if [[ $COMP_CWORD -eq 1 ]]; then
    COMPREPLY=( $(compgen -W "{commands} {flags}" -- "$cur") )
    return
  fi

  case "$prev" in
    --format) COMPREPLY=( $(compgen -W "table json oneline text" -- "$cur") ); return ;;
    --lang) COMPREPLY=( $(compgen -W "en zh-Hant" -- "$cur") ); return ;;
    --provider) COMPREPLY=( $(compgen -W "claude-code codex" -- "$cur") ); return ;;
  esac

  case "${{COMP_WORDS[1]}}" in
    focus) COMPREPLY=() ;;
    hook) COMPREPLY=( $(compgen -W "--provider" -- "$cur") ) ;;
    install-hooks|remove-hooks) COMPREPLY=( $(compgen -W "--dry-run" -- "$cur") ) ;;
    gc) COMPREPLY=( $(compgen -W "--dry-run --older-than --all-ended" -- "$cur") ) ;;
    digest) COMPREPLY=( $(compgen -W "--since --format" -- "$cur") ) ;;
    completion) [[ $COMP_CWORD -eq 2 ]] && COMPREPLY=( $(compgen -W "zsh bash" -- "$cur") ) ;;
    config)
      if [[ $COMP_CWORD -eq 2 ]]; then
        COMPREPLY=( $(compgen -W "get set" -- "$cur") )
      elif [[ $COMP_CWORD -eq 3 ]]; then
        COMPREPLY=( $(compgen -W "{keys}" -- "$cur") )
      fi
      ;;
  esac
}}
complete -F _ring_completion ring
"""


def completion_script(shell: str) -> str | None:
    """生成指定 shell 的補全腳本；不支援的 shell 回 ``None``。"""
    keys = settable_keys()
    if shell == "zsh":
        return _ZSH_TEMPLATE.format(keys=" ".join(keys))
    if shell == "bash":
        return _BASH_TEMPLATE.format(commands=" ".join(_COMMANDS), flags=" ".join(_TOP_FLAGS), keys=" ".join(keys))
    return None


def run_completion(args: list[str]) -> int:
    """``ring completion`` 進入點：印腳本到 stdout；shell 缺漏 / 不支援 → 用法到 stderr、rc 2。"""
    args = _strip_lang(args)
    if len(args) != 1:
        print(_("用法：ring completion zsh|bash"), file=sys.stderr)
        return 2
    script = completion_script(args[0])
    if script is None:
        print(_("不支援的 shell：{shell}（目前支援 zsh / bash）", shell=args[0]), file=sys.stderr)
        return 2
    print(script)
    return 0
