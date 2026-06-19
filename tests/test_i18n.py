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
    assert _("看所有 Claude Code session 上台。") == "Watch all your Claude Code sessions on one stage."


def test_english_plurals() -> None:
    set_lang("en")
    assert ngettext("{n} 個 session 在場", "{n} 個 session 在場", 1, n=1) == "1 session on stage"
    assert ngettext("{n} 個 session 在場", "{n} 個 session 在場", 5, n=5) == "5 sessions on stage"


def test_kwargs_formatting() -> None:
    set_lang("en")
    assert _("→ {project}（{where}）", project="x", where="y") == "→ x (y)"
