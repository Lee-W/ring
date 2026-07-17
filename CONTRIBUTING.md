# Contributing to RiNG

<!-- --8<-- [start:body] -->
謝謝你願意幫 RiNG 變好。這份文件整理本 repo 的開發流程、測試方式與送 PR 前的檢查項目。

## 開發環境

RiNG 需要 Python 3.13+，專案使用 `uv` 管理依賴與執行環境。

```sh
uv sync --all-groups
uv run ring
uv run ring --watch
```

PyPI 發佈名是 `ring-cli`，但 Python module 與 CLI 指令都叫 `ring`。

## 常用命令

```sh
uv run poe format       # ruff check --fix + ruff format
uv run poe lint         # ruff check + mypy
uv run poe test         # pytest
uv run poe cover        # pytest + coverage report
uv run poe all          # format + lint + coverage
uv run poe ci           # pre-commit hooks + coverage
```

測試預設會用 `pytest-xdist` 平行執行；要跑單一測試時可以直接用 pytest：

```sh
uv run pytest tests/test_cli.py
uv run pytest tests/test_cli.py::test_name
```

## 文件建置

文件使用 Material for MkDocs 與 `mkdocs-static-i18n`。`uv sync --all-groups` 會安裝文件依賴；本機預覽與正式檢查分別執行：

```sh
uv run poe docs:serve  # 在本機啟動可自動重載的文件站
uv run poe docs:build  # 以 strict mode 建置到 site/
```

台灣華語是預設語言，來源檔使用 `name.md`；英文翻譯使用 `name.en.md`。兩種語言的文件內連結都應寫成未帶語言 suffix 的路徑，例如 `[Session 狀態](session-states.md)`，由 i18n plugin 自動連到目前語言。新增頁面時也要更新 `mkdocs.yml` 的 `nav`；menu 標題翻譯放在 `nav_translations`。

送出文件變更前請執行 strict build。不要 commit `site/` 產物；GitHub Pages workflow 會從 lockfile 安裝 `docs` dependency group 並重新建置。

## Pre-commit Hooks

建議安裝 hooks，讓格式化、lint、lockfile、commit message 檢查在本機先跑過。

```sh
uv run poe setup-pre-commit
```

目前 hooks 會檢查 TOML/YAML、private key、拼字、`uv.lock`、Ruff、Mypy、Commitizen 等項目。
`no-commit-to-branch` 會擋直接 commit 到受保護分支；必要時請在 feature branch 上工作。

## 程式風格

- Python 程式碼放在 `src/ring/`，測試放在 `tests/`。
- Ruff 目標版本是 Python 3.13，行寬 120。
- Mypy 使用 strict mode；新增 API 時請補齊型別。
- CLI 行為、輸出格式、hook protocol、JSON keys 會被使用者腳本依賴；改動時請優先維持相容，必要 breaking change 要清楚標示。
- 專案刻意允許台灣華語註解與 UI 字串；新增英文 UI 時也要注意 i18n。

## 測試準則

請依改動範圍補測試：

- CLI 參數與輸出：`tests/test_cli.py`
- TUI / render 行為：`tests/test_tui.py`、`tests/test_render.py`
- source discovery：`tests/test_sources.py`、`tests/test_discover.py`
- hook / registry / IPC：`tests/test_hook.py`、`tests/test_registry.py`、`tests/test_ipc.py`
- notifier / focus / permission reply：對應 `tests/test_notify.py`、`tests/test_focus.py`、`tests/test_permission.py`

送 PR 前至少跑：

```sh
uv run poe lint
uv run poe test
```

若改到跨模組行為、輸出格式或發佈相關設定，請跑：

```sh
uv run poe all
```

## i18n

改到使用者可見字串時，請更新翻譯檔。

```sh
uv run poe i18n:extract
uv run poe i18n:check
uv run poe i18n:compile
```

注意：不要用 `pybabel update`。這會把空 `msgstr` 塞進 `.po`，可能弄壞 i18n 測試。新字串請手動把已翻譯條目加進各語言 `.po`，再重新 compile 並 commit `.po` 與 `.mo`。

## Commit 與 Changelog

本專案使用 [Commitizen](https://commitizen-tools.github.io/commitizen/) /
[Conventional Commits](https://www.conventionalcommits.org/)，commit message 例如：

```text
feat: add webhook retry backoff
fix: keep stale codex sessions hidden
docs: document hook setup
test: cover tmux permission parsing
```

版本與 changelog 由 Commitizen 管理。一般 PR 不需要手動改 `CHANGELOG.md`，除非正在做 release / bump。

## PR Checklist

送 PR 前請確認：

- 變更範圍聚焦，沒有混入無關格式化或產物。
- 新功能或修 bug 有對應測試，或在 PR 說明中交代為什麼無法測。
- `uv.lock` 與依賴設定一致。
- 使用者可見字串已處理 i18n。
- README / docs 已同步更新 CLI 旗標、設定鍵、輸出格式或行為變更。
- 文件變更已通過 `uv run poe docs:build`，中英文頁面、導覽與內部連結保持同步。
- 有 breaking change 時，PR 說明清楚列出影響與遷移方式。

## 回報 Issue

請盡量附上：

- 作業系統與終端環境，例如 macOS + iTerm2、tmux、Linux X11 / Wayland。
- `ring --version` 與安裝方式。
- 相關命令與實際輸出。
- 若和 hook / focus / permission reply 有關，請附 `ring doctor` 的相關段落。

請不要貼出含有 token、private key、完整私有 transcript 或其他敏感資訊的內容。
<!-- --8<-- [end:body] -->
