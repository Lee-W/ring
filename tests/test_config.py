from pathlib import Path

from ring.config import Config, load


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    assert load(tmp_path / "nope.toml") == Config()


def test_parses_values(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        'lang = "en"\n'
        "interval = 1.5\n"
        "show_all = true\n"
        "working_threshold_seconds = 30\n"
        "notify_sound = false\n"
        'notify_sound_name = "Ping"\n'
        "notify_repeat_seconds = [10, 20, 60]\n"
        "notify_repeat_max = 0\n"
        'focusers = ["Terminal", "iTerm2"]\n'
    )
    cfg = load(p)
    assert cfg.lang == "en"
    assert cfg.interval == 1.5
    assert cfg.show_all is True
    assert cfg.working_threshold_seconds == 30
    assert cfg.notify_sound is False
    assert cfg.notify_sound_name == "Ping"
    assert cfg.notify_repeat_seconds == (10, 20, 60)
    assert cfg.notify_repeat_max == 0
    assert cfg.focusers == ("Terminal", "iTerm2")


def test_notify_backend_parses_valid(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('notify_backend = "osascript"\n')
    assert load(p).notify_backend == "osascript"


def test_notify_backend_accepts_any_name(tmp_path: Path) -> None:
    """後端名稱由 notify registry 決定，config 不寫死——任意名稱原樣保留（執行期才退回 auto）。"""
    p = tmp_path / "config.toml"
    p.write_text('notify_backend = "notify-send"\n')
    assert load(p).notify_backend == "notify-send"


def test_notify_backend_non_string_falls_back_to_auto(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("notify_backend = 123\n")
    assert load(p).notify_backend == "auto"


def test_bad_types_fall_back_to_defaults(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('interval = "fast"\nfocusers = "nope"\n')
    cfg = load(p)
    assert cfg.interval == Config().interval
    assert cfg.focusers == ()
    assert cfg.notify_repeat_seconds == Config().notify_repeat_seconds


def test_invalid_toml_returns_defaults(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[unclosed")
    assert load(p) == Config()


def test_colors_merge_over_defaults(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[colors]\nwaiting = "magenta"\n')
    cfg = load(p)
    assert cfg.colors["waiting"] == "magenta"  # 覆寫
    assert cfg.colors["working"] == "green"  # 未覆寫 → 預設


def test_colors_default_when_absent(tmp_path: Path) -> None:
    assert load(tmp_path / "none.toml").colors["waiting"] == "bold red"
