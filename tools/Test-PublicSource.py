from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.dont_write_bytecode = True
from public_source import scan_public_tree, verify_public_inventory


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a prepared public-source tree for private state.")
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--allow-git-metadata",action="store_true",
        help="Ignore only the current checkout's .git directory for public-repository CI.",
    )
    args = parser.parse_args()
    root=args.root.resolve()
    # Reject an unprepared or invalid inventory before scanning file contents.
    # In particular, a development checkout without public-source.json is not
    # a public export and must never fall through into recursive content scans.
    findings = verify_public_inventory(root, allow_git_metadata=args.allow_git_metadata)
    if findings:
        raise SystemExit("Public-source inventory is not ready:\n" + "\n".join(findings))
    findings = scan_public_tree(root, allow_git_metadata=args.allow_git_metadata)
    if findings:
        raise SystemExit("Public-source privacy scan failed:\n" + "\n".join(findings))
    print("Public-source privacy scan passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
