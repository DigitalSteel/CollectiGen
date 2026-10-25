from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SEMVER_PATTERN = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def normalized_text(value: str) -> str:
    return value.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def verify_release_notes(canonical: Path, manifest: Path, draft_body: Path) -> None:
    release = json.loads(manifest.read_text(encoding="utf-8-sig"))
    version = str(release.get("version") or "")
    if not SEMVER_PATTERN.fullmatch(version):
        raise ValueError("Release manifest has no valid SemVer version.")
    if canonical.name != f"v{version}.md":
        raise ValueError(f"Canonical release notes must be docs/releases/v{version}.md.")
    expected = normalized_text(canonical.read_text(encoding="utf-8-sig"))
    embedded = normalized_text(str(release.get("release_notes") or ""))
    draft = normalized_text(draft_body.read_text(encoding="utf-8-sig"))
    if expected != embedded:
        raise ValueError("Signed manifest release notes diverge from the canonical notes file.")
    if expected != draft:
        raise ValueError("Draft GitHub Release body diverges from the canonical notes file.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify one canonical release-notes body across release surfaces.")
    parser.add_argument("--canonical", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--draft-body", type=Path, required=True)
    args = parser.parse_args()
    verify_release_notes(args.canonical, args.manifest, args.draft_body)
    print("Canonical release notes match the signed manifest and draft GitHub Release body.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
