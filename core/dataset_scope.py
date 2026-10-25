from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable,Iterator


IMG_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def _clean_path_input(value: str) -> Path:
    """Accept pasted Windows paths, including paths wrapped in quotes."""
    text = value.strip().strip('"').strip("'")
    return Path(text).expanduser()


@dataclass(frozen=True)
class DatasetScope:
    """A directory and recursion choice shared by dataset tools."""

    root: Path
    include_subfolders: bool = True
    excluded_directories: tuple[Path, ...] = ()
    ignored_directory_names: tuple[str, ...] = (
        ".dtrash",
        ".tagmanager-backups",
        "__pycache__",
        "TagMergeManager",
    )

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser()
        if not root.exists():
            raise FileNotFoundError(f"Directory does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Not a directory: {root}")
        object.__setattr__(self, "root", root.resolve())
        object.__setattr__(
            self,
            "excluded_directories",
            tuple(Path(path).expanduser().resolve() for path in self.excluded_directories),
        )

    @classmethod
    def prompt(cls) -> DatasetScope:
        env_root = os.environ.get("TAGMANAGER_DATASET_DIR", "").strip()
        if env_root:
            root = _clean_path_input(env_root)
            if not root.exists() or not root.is_dir():
                raise NotADirectoryError(f"Configured dataset directory is invalid: {root}")
            include_value = os.environ.get("TAGMANAGER_INCLUDE_SUBFOLDERS", "1").strip().lower()
            include_subfolders = include_value not in {"0", "false", "no", "n"}
            problems = os.environ.get("TAGMANAGER_PROBLEMS_DIR", "").strip()
            excluded = (Path(problems),) if problems else ()
            return cls(
                root=root,
                include_subfolders=include_subfolders,
                excluded_directories=excluded,
            )

        while True:
            raw = input("\nDirectory to process: ").strip()
            try:
                root = _clean_path_input(raw)
                if not root.exists():
                    print(f"Directory does not exist: {root}")
                    continue
                if not root.is_dir():
                    print(f"Not a directory: {root}")
                    continue
                break
            except OSError as exc:
                print(f"Invalid directory: {exc}")

        while True:
            choice = input("Include subfolders? (y/n): ").strip().lower()
            if choice in {"y", "yes"}:
                include_subfolders = True
                break
            if choice in {"n", "no"}:
                include_subfolders = False
                break
            print("Please enter y or n.")

        return cls(root=root, include_subfolders=include_subfolders)

    def iter_images(
        self,*,progress: Callable[[int,int,str],None]|None=None,
        should_cancel: Callable[[],bool]|None=None,
    ) -> Iterator[Path]:
        """Enumerate deterministically with optional discovery progress.

        The final total is unknown until traversal finishes, so discovery
        callbacks use ``total=0`` and publish a bounded final count after sort.
        """
        if not self.include_subfolders:
            images=[]
            for path in self.root.iterdir():
                if should_cancel and should_cancel():
                    raise InterruptedError("Image discovery cancelled safely")
                if path.is_file() and path.suffix.lower() in IMG_EXTENSIONS:
                    images.append(path)
                    if progress and len(images)%25==0:
                        progress(len(images),0,path.name)
            images.sort(key=lambda path:str(path).lower())
            if progress:
                progress(len(images),len(images),images[-1].name if images else "No images found")
            yield from images
            return

        images: list[Path] = []
        ignored = {name.casefold() for name in self.ignored_directory_names}
        for current, directories, files in os.walk(self.root):
            if should_cancel and should_cancel():
                raise InterruptedError("Image discovery cancelled safely")
            current_path = Path(current)
            directories[:] = [
                name for name in directories
                if name.casefold() not in ignored
                and not self._is_excluded(current_path / name)
            ]
            if self._is_excluded(current_path):
                continue
            for name in files:
                if should_cancel and should_cancel():
                    raise InterruptedError("Image discovery cancelled safely")
                if Path(name).suffix.lower() in IMG_EXTENSIONS:
                    images.append(current_path/name)
                    if progress and len(images)%25==0:
                        progress(len(images),0,name)
        images.sort(key=lambda path:str(path).lower())
        if progress:
            progress(len(images),len(images),images[-1].name if images else "No images found")
        yield from images

    def _is_excluded(self, path: Path) -> bool:
        resolved = path.resolve()
        return any(
            resolved == excluded or excluded in resolved.parents
            for excluded in self.excluded_directories
        )

    def iter_image_caption_pairs(self) -> Iterator[tuple[Path, Path]]:
        for image in self.iter_images():
            caption = image.with_suffix(".txt")
            if caption.is_file():
                yield image, caption

    def iter_sidecars(self) -> Iterator[Path]:
        if not self.include_subfolders:
            yield from sorted(self.root.glob("*.tags.json"), key=lambda path: str(path).lower())
            return
        sidecars: list[Path] = []
        ignored = {name.casefold() for name in self.ignored_directory_names}
        for current, directories, files in os.walk(self.root):
            current_path = Path(current)
            directories[:] = [
                name for name in directories
                if name.casefold() not in ignored
                and not self._is_excluded(current_path / name)
            ]
            if self._is_excluded(current_path):
                continue
            sidecars.extend(
                current_path / name for name in files if name.endswith(".tags.json")
            )
        yield from sorted(sidecars, key=lambda path: str(path).lower())
