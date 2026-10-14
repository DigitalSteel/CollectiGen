from __future__ import annotations

import json
import shutil
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .tag_collection import normalize_tag


MISSING_TAG_EXCEPTION_SCHEMA_VERSION = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MissingTagSearch:
    target: str
    required: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    mode: str = "missing"

    @property
    def key(self) -> str:
        required=",".join(sorted(normalize_tag(value) for value in self.required))
        absent=",".join(sorted(normalize_tag(value) for value in self.absent))
        return f"mode={self.mode}|target={normalize_tag(self.target)}|required={required}|absent={absent}"


class MissingTagExceptionStore:
    """Remembers images rejected for one exact missing-tag search."""

    def __init__(self, path: str | Path) -> None:
        self.path=Path(path); self.rules: dict[str,set[str]]={}
        self.schema_version=MISSING_TAG_EXCEPTION_SCHEMA_VERSION
        self._payload_extras={}; self._source_error=""; self._recovery_error=""
        self.load()

    @property
    def source_error(self) -> str:
        return self._source_error

    @property
    def backup_root(self) -> Path:
        return self.path.parent/"backups"/"missing-tag-exceptions"

    @staticmethod
    def _atomic_bytes(path: Path,content: bytes) -> None:
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix(path.suffix+".restore.tmp")
        temporary.write_bytes(content); temporary.replace(path)

    @staticmethod
    def _atomic_json(path: Path,payload: dict) -> None:
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix(path.suffix+".tmp")
        temporary.write_text(
            json.dumps(payload,indent=2,ensure_ascii=False)+"\n",encoding="utf-8",
        )
        temporary.replace(path)

    def _recover_pending(self) -> None:
        if not self.backup_root.is_dir():return
        target=self.path.resolve()
        for manifest_path in sorted(self.backup_root.glob("*/recovery.json")):
            try:manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError,json.JSONDecodeError,TypeError):continue
            if not isinstance(manifest,dict) or manifest.get("status")!="pending":
                continue
            files=manifest.get("files")
            if not isinstance(files,list) or len(files)!=1 or not isinstance(files[0],dict):
                continue
            entry=files[0]
            try:manifest_target=Path(str(entry.get("target") or "")).resolve()
            except (OSError,RuntimeError,ValueError):continue
            if manifest_target!=target:continue
            existed=bool(entry.get("existed")); backup_name=str(entry.get("backup") or "")
            if backup_name and Path(backup_name).name!=backup_name:
                raise RuntimeError("the recovery backup name is unsafe")
            if existed:
                backup=manifest_path.parent/backup_name
                if not backup.is_file():
                    raise RuntimeError("the exact recovery backup is unavailable")
                self._atomic_bytes(self.path,backup.read_bytes())
            elif self.path.exists():
                self.path.unlink()
            manifest["status"]="recovered"; manifest["recovered_at"]=_now()
            self._atomic_json(manifest_path,manifest)

    def load(self) -> None:
        self.rules={}; self.schema_version=MISSING_TAG_EXCEPTION_SCHEMA_VERSION
        self._payload_extras={}; raw=None; self._recovery_error=""
        try:self._recover_pending()
        except (OSError,RuntimeError,TypeError,ValueError) as exc:
            self._recovery_error=(
                "Remembered missing-tag decisions need recovery before they can "
                f"be used: {exc}. Restore access, then refresh Dataset."
            )
        source_error=self._recovery_error
        try:raw=self.path.read_bytes() if self.path.exists() else None
        except OSError:
            source_error=(
                "Remembered missing-tag decisions are unavailable. The file was "
                "left unchanged; restore access, then refresh Dataset."
            )
        try:payload=json.loads(raw.decode("utf-8")) if raw is not None else {}
        except (UnicodeDecodeError,json.JSONDecodeError,TypeError):
            payload={}
            source_error=(
                "Remembered missing-tag decisions are malformed. The file was left "
                "unchanged; restore a valid copy, then refresh Dataset."
            )
        if not isinstance(payload,dict):
            payload={}
            source_error=(
                "Remembered missing-tag decisions are malformed because their top "
                "level is not an object. The file was left unchanged; restore a "
                "valid copy, then refresh Dataset."
            )
        version=payload.get("schema_version",1)
        if (
            isinstance(version,bool) or not isinstance(version,int)
            or version<1 or version>MISSING_TAG_EXCEPTION_SCHEMA_VERSION
        ):
            source_error=(
                "Remembered missing-tag decisions use an unsupported schema. The "
                "file was left unchanged; update CollectiGen or restore a supported "
                "copy, then refresh Dataset."
            )
        search_exceptions=payload.get("search_exceptions",{})
        if not isinstance(search_exceptions,dict):
            source_error=(
                "Remembered missing-tag decisions are malformed. The file was left "
                "unchanged; restore a valid copy, then refresh Dataset."
            )
            search_exceptions={}
        parsed={}
        for key,values in search_exceptions.items():
            if not isinstance(values,list) or any(not isinstance(value,str) for value in values):
                source_error=(
                    "Remembered missing-tag decisions contain an unsupported record. "
                    "The file was left unchanged; restore a valid copy, then refresh Dataset."
                )
                parsed={}; break
            parsed[str(key)]={str(value) for value in values}
        self.rules=parsed
        if isinstance(version,int) and not isinstance(version,bool):
            self.schema_version=version
        self._payload_extras={
            key:deepcopy(value) for key,value in payload.items()
            if key not in {"schema_version","search_exceptions"}
        }
        self._loaded_bytes=raw; self._source_error=source_error

    def require_readable(self) -> None:
        if self._source_error:raise RuntimeError(self._source_error)

    def validate_current(self) -> bytes | None:
        self.require_readable()
        try:current=self.path.read_bytes() if self.path.exists() else None
        except OSError as exc:
            self.load()
            raise RuntimeError(
                "Remembered missing-tag decisions became unavailable. Nothing was "
                "changed; restore access, then refresh Dataset."
            ) from exc
        if current!=getattr(self,"_loaded_bytes",None):
            self.load()
            raise RuntimeError(
                "Remembered missing-tag decisions changed after this action opened. "
                "The newer file was left unchanged and reloaded; review it before retrying."
            )
        return current

    def _payload(self) -> dict:
        payload=deepcopy(self._payload_extras)
        payload.update({
            "schema_version":MISSING_TAG_EXCEPTION_SCHEMA_VERSION,
            "search_exceptions":{
                key:sorted(values) for key,values in sorted(self.rules.items())
            },
        })
        return payload

    def _save_atomic(self) -> None:
        self._atomic_json(self.path,self._payload())
        self._loaded_bytes=self.path.read_bytes()
        self.schema_version=MISSING_TAG_EXCEPTION_SCHEMA_VERSION
        self._source_error=""

    def save_verified(self, *, backup_root: str | Path | None = None) -> Path:
        current=self.validate_current()
        stamp=datetime.now().strftime("%Y%m%d_%H%M%S")
        root=Path(backup_root) if backup_root is not None else self.backup_root
        backup=root/f"{stamp}_{uuid4().hex[:8]}"; backup.mkdir(parents=True,exist_ok=False)
        backup_name=f"0_{self.path.name}"; saved=backup/backup_name
        if current is not None:shutil.copy2(self.path,saved)
        manifest_path=backup/"recovery.json"
        manifest={
            "status":"pending","created_at":_now(),
            "files":[{
                "target":str(self.path.resolve()),"backup":backup_name,
                "existed":current is not None,
            }],
        }
        self._atomic_json(manifest_path,manifest); expected=self._payload()
        try:
            self._save_atomic()
            actual=json.loads(self.path.read_text(encoding="utf-8"))
            if actual!=expected:
                raise RuntimeError(
                    "Saved remembered missing-tag decisions failed read-back verification."
                )
            manifest["status"]="complete"; self._atomic_json(manifest_path,manifest)
        except BaseException:
            if current is None:
                if self.path.exists():self.path.unlink()
            else:self._atomic_bytes(self.path,current)
            self.load(); manifest["status"]="restored"
            manifest["restored_at"]=_now(); self._atomic_json(manifest_path,manifest)
            raise
        return backup

    def save(self) -> None:
        """Compatibility writer; all direct saves retain verified guarantees."""
        try:
            self.save_verified()
        except BaseException:
            self.load()
            raise

    def _mutate_verified(self,mutator):
        self.require_readable()
        before_rules=deepcopy(self.rules); before_version=self.schema_version
        before_extras=deepcopy(self._payload_extras)
        try:result=mutator()
        except BaseException:
            self.rules=before_rules; self.schema_version=before_version
            self._payload_extras=before_extras; raise
        if (
            self.rules==before_rules
            and self.schema_version==before_version
            and self._payload_extras==before_extras
        ):
            return result
        try:self.save_verified()
        except BaseException:
            self.load(); raise
        return result

    @staticmethod
    def _legacy_relative(value: str) -> str:
        text=str(value).strip().replace("\\","/"); path=PurePosixPath(text)
        if (
            not text or text.startswith("/") or ":" in path.parts[0]
            or any(part in {"",".",".."} for part in path.parts)
        ):
            raise RuntimeError(
                "A legacy remembered decision has an unsafe or ambiguous image path. "
                "The exception file was left unchanged for review."
            )
        return path.as_posix().casefold()

    def bind_legacy_library(self,library_id: str) -> int:
        """Bind pre-federation relative keys to the former active library once."""
        self.require_readable(); library_id=str(library_id or "")
        if not library_id or self.schema_version>=MISSING_TAG_EXCEPTION_SCHEMA_VERSION:
            return 0
        def mutate():
            changed=0
            for search_key,values in tuple(self.rules.items()):
                migrated={f"{library_id}:{self._legacy_relative(value)}" for value in values}
                changed+=len(values); self.rules[search_key]=migrated
            self.schema_version=MISSING_TAG_EXCEPTION_SCHEMA_VERSION
            return changed
        return self._mutate_verified(mutate)

    @staticmethod
    def image_key(
        image: str | Path,dataset_root: str | Path,library_id: str="",
    ) -> str:
        image=Path(image); root=Path(dataset_root); library_id=str(library_id or "")
        try:relative=image.resolve().relative_to(root.resolve()).as_posix().casefold()
        except ValueError:
            if library_id:
                raise ValueError(
                    "The image is outside its named library, so its remembered "
                    "decision identity cannot be saved. Refresh Dataset and retry."
                )
            return str(image.resolve()).casefold()
        return f"{library_id}:{relative}" if library_id else relative

    def hidden(
        self,search: MissingTagSearch,image: str | Path,dataset_root: str | Path,
        library_id: str="",
    ) -> bool:
        self.require_readable(); values=self.rules.get(search.key,set())
        return self.image_key(image,dataset_root,library_id) in values

    def update_keys(self,search: MissingTagSearch,*,hide=(),show=()) -> None:
        hidden={str(value) for value in hide}; shown={str(value) for value in show}
        def mutate():
            values=set(self.rules.get(search.key,set()))
            values.difference_update(shown); values.update(hidden)
            if values:self.rules[search.key]=values
            else:self.rules.pop(search.key,None)
        self._mutate_verified(mutate)

    def hide(
        self,search: MissingTagSearch,images,dataset_root: str | Path,
        library_id: str="",
    ) -> None:
        self.update_keys(search,hide=(
            self.image_key(image,dataset_root,library_id) for image in images
        ))

    def show(
        self,search: MissingTagSearch,images,dataset_root: str | Path,
        library_id: str="",
    ) -> None:
        """Forget prior exceptions when an image is deliberately changed."""
        self.update_keys(search,show=(
            self.image_key(image,dataset_root,library_id) for image in images
        ))

    def count(self, search: MissingTagSearch) -> int:
        self.require_readable(); return len(self.rules.get(search.key,set()))

    def clear(self, search: MissingTagSearch) -> int:
        count=len(self.rules.get(search.key,set()))
        self._mutate_verified(lambda:self.rules.pop(search.key,None))
        return count

    def searches_for_image(
        self,image: str | Path,dataset_root: str | Path,library_id: str="",
    ) -> list[str]:
        """Return exact search keys currently hiding one image."""
        self.require_readable(); image_keys={self.image_key(image,dataset_root,library_id)}
        return sorted(
            key for key,values in self.rules.items() if image_keys.intersection(values)
        )

    def forget_image(
        self,image: str | Path,dataset_root: str | Path,
        search_keys: list[str] | None=None,library_id: str="",
    ) -> int:
        """Forget selected missing-tag decisions for one image."""
        image_keys={self.image_key(image,dataset_root,library_id)}
        wanted=set(self.rules if search_keys is None else search_keys)
        def mutate():
            changed=0
            for key in list(self.rules):
                matches=image_keys.intersection(self.rules[key])
                if key not in wanted or not matches:continue
                self.rules[key].difference_update(matches); changed+=1
                if not self.rules[key]:self.rules.pop(key,None)
            return changed
        return self._mutate_verified(mutate)
