# Contributing to RiNG

Thanks for wanting to help make RiNG better. This page covers the dev workflow, how to test, and
what to check before opening a PR.

## Dev environment

RiNG needs Python 3.13+; the project manages dependencies and the run environment with `uv`.

```sh
uv sync --all-groups
uv run ring
uv run ring --watch
```

The PyPI distribution name is `ring-cli`, but the Python module and CLI command are both `ring`.

## Common commands

```sh
uv run poe format       # ruff check --fix + ruff format
uv run poe lint         # ruff check + mypy
uv run poe test         # pytest
uv run poe cover        # pytest + coverage report
uv run poe all          # format + lint + coverage
uv run poe ci           # pre-commit hooks + coverage
```

Tests run in parallel via `pytest-xdist` by default; to run a single test, invoke pytest directly:

```sh
uv run pytest tests/test_cli.py
uv run pytest tests/test_cli.py::test_name
```

## Documentation

The site uses Material for MkDocs and `mkdocs-static-i18n`. `uv sync --all-groups` installs the documentation dependencies. Preview and validate the site with:

```sh
uv run poe docs:serve  # local server with live reload
uv run poe docs:build  # strict build into site/
```

Taiwanese Mandarin is the default language and uses `name.md`; English translations use `name.en.md`. Links in both languages must omit the locale suffix—for example, `[Session states](session-states.md)`—so the i18n plugin resolves the current language. When adding a page, update `nav` in `mkdocs.yml` and put menu-label translations under `nav_translations`.

Run the strict build before submitting documentation changes. Do not commit `site/`; the GitHub Pages workflow installs the locked `docs` dependency group and rebuilds it.

## Pre-commit hooks

Installing the hooks is recommended, so formatting, lint, lockfile, and commit-message checks all
run locally first.

```sh
uv run poe setup-pre-commit
```

The hooks currently check TOML/YAML, private keys, spelling, `uv.lock`, Ruff, Mypy, Commitizen,
and more. `no-commit-to-branch` blocks direct commits to protected branches — work on a feature
branch when needed.

## Code style

- Python source lives in `src/ring/`, tests in `tests/`.
- Ruff targets Python 3.13, line width 120.
- Mypy runs in strict mode; add complete types for any new API.
- CLI behavior, output formats, the hook protocol, and JSON keys are relied on by users' own
  scripts — prefer compatibility when changing them, and clearly flag any necessary breaking
  change.
- The project deliberately allows Taiwanese Mandarin comments and UI strings; mind i18n when
  adding new English-facing UI too.

## Testing guidelines

Add tests scoped to what you changed:

- CLI args and output: `tests/test_cli.py`
- TUI / render behavior: `tests/test_tui.py`, `tests/test_render.py`
- Source discovery: `tests/test_sources.py`, `tests/test_discover.py`
- Hook / registry / IPC: `tests/test_hook.py`, `tests/test_registry.py`, `tests/test_ipc.py`
- Notifier / focus / permission reply: `tests/test_notify.py`, `tests/test_focus.py`,
  `tests/test_permission.py` respectively

Run at least this before opening a PR:

```sh
uv run poe lint
uv run poe test
```

If the change touches cross-module behavior, output formats, or release-related settings, run:

```sh
uv run poe all
```

## i18n

If your change touches user-facing strings, update the translation files.

```sh
uv run poe i18n:extract
uv run poe i18n:check
uv run poe i18n:compile
```

Note: don't use `pybabel update` — it stuffs empty `msgstr` entries into `.po` files and can break
the i18n tests. Add new strings' translated entries by hand to each language's `.po` file, then
recompile and commit both the `.po` and `.mo` files.

## Commits and changelog

This project uses [Commitizen](https://commitizen-tools.github.io/commitizen/) /
[Conventional Commits](https://www.conventionalcommits.org/), e.g.:

```text
feat: add webhook retry backoff
fix: keep stale codex sessions hidden
docs: document hook setup
test: cover tmux permission parsing
```

Versioning and the changelog are managed by Commitizen. Regular PRs shouldn't hand-edit
`CHANGELOG.md` unless you're doing a release / bump.

## PR checklist

Before opening a PR, confirm:

- The change is scoped tightly, with no unrelated formatting or build artifacts mixed in.
- New features or bug fixes have matching tests, or the PR description explains why they can't be
  tested.
- `uv.lock` is consistent with the dependency configuration.
- User-facing strings have been run through i18n.
- README / docs have been updated for any changed CLI flags, config keys, output format, or
  behavior.
- Documentation changes pass `uv run poe docs:build`, with translated pages, navigation, and
  internal links kept in sync.
- Any breaking change is called out clearly in the PR description, with its impact and migration
  path.

## Reporting an issue

Please include, where possible:

- OS and terminal environment, e.g. macOS + iTerm2, tmux, Linux X11 / Wayland.
- `ring --version` and how it was installed.
- The relevant commands and their actual output.
- If it's related to hooks / focus / permission reply, the relevant section of `ring doctor`.

Please don't paste content containing tokens, private keys, full private transcripts, or other
sensitive information.
