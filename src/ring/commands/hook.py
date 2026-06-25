"""Hook-related CLI command handlers."""

from __future__ import annotations


def run_hook_command(args: list[str]) -> int:
    from ring.hook import run_hook

    provider = "claude-code"
    if args:
        if args[0] == "--provider" and len(args) >= 2:
            provider = args[1]
        elif args[0].startswith("--provider="):
            provider = args[0].split("=", 1)[1]
        elif not args[0].startswith("-"):
            provider = args[0]
    return run_hook(provider=provider)


def run_install_hooks(args: list[str]) -> int:
    from ring.hook import install_hooks

    return install_hooks(dry_run="--dry-run" in args)


def run_remove_hooks(args: list[str]) -> int:
    from ring.hook import uninstall_hooks

    return uninstall_hooks(dry_run="--dry-run" in args)
