"""Create-only manual prompt export; never replace existing data or source files."""
from pathlib import Path
from .training_source_io import _assert_unredirected


def export_new_prompt_text(path, positive, negative, *, policy, protected_paths=()):
    for text in (positive, negative):
        if not isinstance(text, str) or len(text.encode('utf-8')) > 16384 or '\x00' in text:
            raise ValueError("Prompt text must be bounded Unicode without NUL.")
    target = Path(path).absolute()
    _assert_unredirected(target.parent)
    _assert_unredirected(target)
    for source in protected_paths:
        if source and target.resolve() in {Path(source).resolve(), Path(source).with_suffix('.tags.json').resolve(),
                                          Path(source).with_suffix('.txt').resolve()}:
            raise ValueError("Choose a separate prompt export, outside the image and its metadata files.")
    payload = ("Positive prompt:\n" + positive + "\n\nNegative prompt:\n" + negative +
               "\n\nAuthoring warning:\n" + str(policy) +
               "\nPlain text does not contain original metadata or complete draft provenance.\n").encode('utf-8')
    # atomic_verified_bytes replaces targets; create-only export instead uses stock exclusive creation.
    # There is no prior authority to back up or replace. A failed file is retained for exact recovery.
    with target.open('xb') as handle:
        handle.write(payload)
        handle.flush()
        import os
        os.fsync(handle.fileno())
    _assert_unredirected(target)
    if target.read_bytes() != payload:
        raise RuntimeError("Export read-back failed; the new file was retained. Choose a new filename to retry.")
    return target
