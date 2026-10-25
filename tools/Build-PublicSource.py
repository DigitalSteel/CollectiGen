from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from public_source import PublicSourceError, main


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PublicSourceError as exc:
        print(f"Public source export refused: {exc}")
        raise SystemExit(1)
