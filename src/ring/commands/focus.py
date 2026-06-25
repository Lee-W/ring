"""``ring focus`` command handler."""

from __future__ import annotations


def run_focus(args: list[str]) -> int:
    if len(args) < 1:
        return 0

    from ring.focus import jump as focus_jump
    from ring.ipc import read_tui_presence, write_focus_request
    from ring.sources import get_by_id

    session_id = args[0]
    session = get_by_id(session_id)
    if session is None:
        return 0
    presence = read_tui_presence()
    if presence is not None:
        write_focus_request(session_id)
    else:
        focus_jump(session)
    return 0
