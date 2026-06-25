"""CLI argument helpers shared by subcommands."""

from __future__ import annotations


def strip_lang(args: list[str]) -> list[str]:
    """濾掉全域 ``--lang`` 旗標（已在 main 先 peek 過），只留子命令自己的參數。"""
    out: list[str] = []
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "--lang":
            skip = i + 1 < len(args)
            continue
        if a.startswith("--lang="):
            continue
        out.append(a)
    return out
