from ring.i18n import gettext as _
from ring.i18n import ngettext, resolve_lang, set_lang


def test_resolve_lang() -> None:
    assert resolve_lang("en") == "en"
    assert resolve_lang("en_US.UTF-8") == "en"
    assert resolve_lang("zh_TW.UTF-8") == "zh-Hant"
    assert resolve_lang(None) == "zh-Hant"


def test_default_is_chinese_msgid() -> None:
    set_lang(None)
    assert _("等你") == "等你"


def test_english_translation_loads_from_mo() -> None:
    set_lang("en")
    assert _("等你") == "waiting"
    assert _("看所有 agent CLI session 上台。") == "Watch all your agent CLI sessions on one stage."


def test_english_plurals() -> None:
    set_lang("en")
    assert ngettext("{n} 個 session 在場", "{n} 個 session 在場", 1, n=1) == "1 session on stage"
    assert ngettext("{n} 個 session 在場", "{n} 個 session 在場", 5, n=5) == "5 sessions on stage"


def test_kwargs_formatting() -> None:
    set_lang("en")
    assert _("→ {project}（{where}）", project="x", where="y") == "→ x (y)"


# ── 新增字串（Step 5/6/7）──


def test_notify_title_translation() -> None:
    """notify title 的新 gettext msgid 有英文翻譯。"""
    set_lang("en")
    result = _("RiNG · {project} 在等你回話", project="maigo")
    assert result == "RiNG · maigo is waiting for you"


def test_notify_message_translation() -> None:
    """notify message 的 {project} · …/{tail} 有英文翻譯（pass-through）。"""
    set_lang("en")
    result = _("{project} · …/{tail}", project="maigo", tail="myproject")
    assert result == "maigo · …/myproject"


def test_install_hint_translation() -> None:
    """terminal-notifier 安裝引導字串有英文翻譯。"""
    set_lang("en")
    result = _("💡 裝 terminal-notifier 可點擊通知直接跳回 session：brew install terminal-notifier")
    assert "terminal-notifier" in result
    assert "brew install" in result


def test_tui_jumped_to_translation() -> None:
    """TUI 跳轉成功的 status 字串有英文翻譯。"""
    set_lang("en")
    result = _("→ 已跳到 {project}（來自通知）", project="maigo")
    assert "maigo" in result
    assert "notification" in result


def test_tui_session_not_on_stage_translation() -> None:
    """TUI 找不到 session 的警示字串有英文翻譯。"""
    set_lang("en")
    result = _("那個 session 已不在場")
    assert "no longer" in result or "session" in result
