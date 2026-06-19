"""讓 `python -m ring` 等同於 `ring`。"""

from ring.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
