"""``ring focus`` command handler."""

from __future__ import annotations

import sys

from ring.i18n import gettext as _
from ring.registry import Session


def _resolve_session(query: str) -> tuple[Session | None, str | None, int]:
    from ring.sources import discover_sessions, get_by_id

    exact = get_by_id(query)
    if exact is not None:
        return exact, None, 0

    matches = [s for s in discover_sessions() if s.session_id.startswith(query)]
    if len(matches) == 1:
        return matches[0], None, 0
    if len(matches) > 1:
        sample = ", ".join(s.session_id for s in matches[:5])
        more = _(" 等 {n} 筆", n=len(matches)) if len(matches) > 5 else ""
        return (
            None,
            _("session id 前綴不唯一：{query}（符合：{matches}{more}）", query=query, matches=sample, more=more),
            2,
        )
    return None, _("找不到 session：{query}", query=query), 1


def run_focus(args: list[str]) -> int:
    if len(args) < 1:
        print(_("用法：ring focus SESSION_ID"), file=sys.stderr)
        return 2

    from ring.focus import jump as focus_jump
    from ring.ipc import read_tui_presence, write_focus_request

    query = args[0]
    session, error, error_code = _resolve_session(query)
    if session is None:
        print(error or _("找不到 session：{query}", query=query), file=sys.stderr)
        return error_code
    presence = read_tui_presence()
    if presence is not None:
        write_focus_request(session.session_id)
    else:
        ok, message = focus_jump(session)
        if not ok:
            print(message, file=sys.stderr)
            return 1
    return 0
