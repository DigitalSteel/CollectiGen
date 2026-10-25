"""Direct-source Organizer launcher for an explicitly selected private interpreter."""
from pathlib import Path
import sys


def main(argv=None):
    # Code location only. Runtime policy, runtime root and data profile remain
    # explicit CLI inputs; no site-packages or runtime-manifest inference.
    class OwnedProgramPath(str):
        pass
    marker=OwnedProgramPath(str(Path(__file__).resolve().parents[2]))
    sys.path.insert(0,marker)
    try:
        from suite_core.media_organizer.__main__ import cli_main
        return cli_main(argv)
    finally:
        slots=[index for index,value in enumerate(sys.path) if value is marker]
        if len(slots)!=1:
            raise RuntimeError("Organizer launcher source-path ownership changed.")
        del sys.path[slots[0]]


if __name__=='__main__':
    raise SystemExit(main())
