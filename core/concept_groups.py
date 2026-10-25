"""Named training collections with recoverable membership; never image facts."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from uuid import uuid4

from .external_write_transaction import atomic_verified_bytes
from .federated_dataset import LibraryImageIdentity
from .model_resource_registry import _bounded_read, _json
from .training_source_io import _assert_unredirected, _identity

MAX_BYTES = 16 * 1024 * 1024
MAX_GROUPS = 500
MAX_MEMBERS = 50_000
MAX_ADD = 5_000


class ConceptGroupError(ValueError):
    pass


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _hash(raw):
    return hashlib.sha256(raw or b"").hexdigest()


def group_fingerprint(group):
    return _hash(_bytes(group))


def _text(value, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or "\0" in value or (required and not value.strip()):
        raise ConceptGroupError("Enter a bounded collection name and description.")
    return value.strip()


def _member(value):
    if not isinstance(value, dict):
        raise ConceptGroupError("Collection membership is invalid.")
    library = _text(value.get("library_id"), 160, required=True)
    relative = value.get("relative_path")
    if (not isinstance(relative, str) or not relative or len(relative)>2000
            or "\\" in relative or ":" in relative or "\0" in relative
            or relative.startswith("/") or any(p in {"", ".", ".."} for p in relative.split("/"))
            or PurePosixPath(relative).is_absolute()):
        raise ConceptGroupError("Collection images need safe library-relative identities.")
    fingerprint = value.get("image_sha256")
    if not isinstance(fingerprint, str) or not re.fullmatch("[0-9a-f]{64}", fingerprint):
        raise ConceptGroupError("Collection images need verified image hashes.")
    return {**deepcopy(value), "library_id": library, "relative_path": relative, "image_sha256": fingerprint}


def _key(member):
    return (member["library_id"].casefold(), member["relative_path"].casefold())


def _validate(value):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] not in {1,2}:
        raise ConceptGroupError("Training collections use an unsupported format; saved data was left unchanged.")
    groups = value.get("groups")
    if not isinstance(groups, dict) or len(groups)>MAX_GROUPS:
        raise ConceptGroupError("Training collections exceed the supported collection limit.")
    names=set(); count=0
    for key, group in groups.items():
        if not isinstance(key,str) or not re.fullmatch(r"concept\.[0-9a-f]{32}",key) or not isinstance(group,dict) or group.get("id")!=key:
            raise ConceptGroupError("A saved collection identity is invalid.")
        name=_text(group.get("name"),120,required=True)
        _text(group.get("description"),4000)
        _text(group.get("created_at"),80,required=True); _text(group.get("updated_at"),80,required=True)
        if name.casefold() in names:
            raise ConceptGroupError("A collection with this name already exists.")
        names.add(name.casefold())
        if not isinstance(group.get("members"),list):
            raise ConceptGroupError("A collection has invalid image membership.")
        rules=group.get("rules")
        ignored=group.get("ignored_members",[])
        if value["schema_version"]==1 and (rules is not None or ignored):
            raise ConceptGroupError("Collection membership needs the newer saved format.")
        if rules is not None:
            from .collection_rules import normalize_collection_rules
            if rules!=normalize_collection_rules(rules):
                raise ConceptGroupError("Collection rules are not in their canonical format.")
        if not isinstance(ignored,list):
            raise ConceptGroupError("Ignored collection membership is invalid.")
        seen=set()
        for row in group["members"]:
            member=_member(row); identity=_key(member)
            origin=member.get("membership_source","manual")
            if origin not in {"manual","automatic"} or (origin=="automatic" and rules is None):
                raise ConceptGroupError("Collection membership has an unsupported source.")
            if identity in seen:
                raise ConceptGroupError("A collection contains a duplicate image identity.")
            seen.add(identity)
        ignored_seen=set()
        for row in ignored:
            identity=_key(_member(row))
            if identity in seen or identity in ignored_seen:
                raise ConceptGroupError("A collection cannot include and ignore the same image.")
            ignored_seen.add(identity)
        count+=len(seen)+len(ignored_seen)
        if count>MAX_MEMBERS:
            raise ConceptGroupError("Collection membership exceeds the supported collection limit.")
    return value


class ConceptGroupStore:
    def __init__(self,path,*,history_factory=None):
        self.path=Path(path).absolute()
        self.pending=self.path.with_name(self.path.name+".pending.json")
        self.history_factory=history_factory

    def _history(self):
        adapter=self.history_factory() if self.history_factory is not None else None
        if adapter is not None: adapter.require_path(self.path)
        return adapter

    def _history_guard(self,adapter):
        if adapter is not None: return adapter.history.locked()
        if self.history_factory is None: return nullcontext()
        guard=getattr(self.history_factory,"off_guard",None)
        if not callable(guard):
            raise ConceptGroupError("Application Collection history needs saved-settings admission before an Off write.")
        return guard()

    def _read(self,path=None,limit=MAX_BYTES):
        return _bounded_read(path or self.path,limit)

    def snapshot(self):
        if self._read(self.pending,4096) is not None:
            raise ConceptGroupError("A collection save was interrupted. Use Recover Saved Collections before continuing.")
        raw=self._read()
        value=_validate(_json(raw) if raw is not None else {"schema_version":1,"groups":{}})
        return deepcopy(value),_hash(raw)

    @contextmanager
    def locked_snapshot(self):
        """Hold the native Collection writer lease for an exact read/admission.

        Cooperating writers fail rather than change membership until exit. This
        writes only the canonical lock file and does not mutate Collection data.
        """
        with self._writer():
            yield self.snapshot()

    @contextmanager
    def _writer(self):
        _assert_unredirected(self.path.parent)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        lock=self.path.with_name(self.path.name+".lock"); _assert_unredirected(lock)
        fd=os.open(lock,os.O_RDWR|os.O_CREAT|getattr(os,"O_BINARY",0)|getattr(os,"O_NOFOLLOW",0),0o600)
        with os.fdopen(fd,"r+b") as handle:
            state=os.fstat(handle.fileno())
            if not stat.S_ISREG(state.st_mode) or _identity(state)!=_identity(lock.stat(follow_symlinks=False)):
                raise ConceptGroupError("Concept lock changed; reload before editing.")
            if not state.st_size:
                handle.write(b"0"); handle.flush(); os.fsync(handle.fileno())
            handle.seek(0)
            try:
                if os.name=="nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError as exc:
                raise ConceptGroupError("Another collection update is active; retry shortly.") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name=="nt": msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else: fcntl.flock(handle.fileno(),fcntl.LOCK_UN)

    def _write_bytes(self,path,raw):
        _assert_unredirected(path.parent); _assert_unredirected(path)
        atomic_verified_bytes(path,raw,label="Collection state")
        _assert_unredirected(path)

    def _commit(self,value,expected,*,history=None,group_id=None,restored_from=None):
        before=self._read()
        if _hash(before)!=expected:
            raise ConceptGroupError("Training collections changed after this view opened. Reload and retry.")
        after=_bytes(_validate(value))
        if len(after)>MAX_BYTES: raise ConceptGroupError("Training collections exceed the supported storage size.")
        if before==after: return
        op=uuid4().hex
        prepared=history.prepare(before,after,group_id,op,restored_from=restored_from) if history else None
        recovery=self.path.parent/"concept-group-recovery"/op
        _assert_unredirected(recovery); recovery.mkdir(parents=True,exist_ok=False)
        self._write_bytes(recovery/"before.json",before or b"")
        self._write_bytes(recovery/"after.json",after)
        receipt={"schema_version":1,"operation_id":op,"target":self.path.name,
                 "before_absent":before is None,"before_sha256":_hash(before),"after_sha256":_hash(after)}
        if prepared is not None:
            receipt.update(schema_version=2,history_scope=prepared.scope_key,
                history_transaction=prepared.transaction_id,history_group=group_id)
        self._write_bytes(recovery/"receipt.json",_bytes(receipt))
        self._write_bytes(self.pending,_bytes(receipt))
        if _hash(self._read())!=expected:
            raise ConceptGroupError("Collection state changed during save. Recover Saved Collections before continuing.")
        self._write_bytes(self.path,after)
        if history is None: self._recover_locked()
        else: self._recover_locked(history=history)

    def recover(self):
        history=self._history()
        with self._history_guard(history):
            with self._writer():
                if self._read(self.pending,4096) is None and history is not None:
                    return history.recover_unlinked(self)
                return self._recover_locked(history=history) if history else self._recover_locked()

    def _recover_locked(self,*,history=None):
        raw=self._read(self.pending,4096)
        if raw is None: return "No interrupted collection save."
        receipt=_json(raw)
        if (not isinstance(receipt,dict) or type(receipt.get("schema_version")) is not int
                or receipt.get("schema_version") not in ({1,2} if history is not None else {1})
                or receipt.get("target")!=self.path.name
                or not re.fullmatch(r"[0-9a-f]{32}",str(receipt.get("operation_id","")))
                or type(receipt.get("before_absent")) is not bool):
            raise ConceptGroupError("Collection recovery evidence is invalid; saved files were preserved.")
        recovery=self.path.parent/"concept-group-recovery"/receipt["operation_id"]
        if self._read(recovery/"receipt.json",4096)!=raw:
            raise ConceptGroupError("Collection recovery receipt changed.")
        before=self._read(recovery/"before.json"); after=self._read(recovery/"after.json")
        if (before is None or after is None or _hash(before)!=receipt.get("before_sha256")
                or _hash(after)!=receipt.get("after_sha256") or (receipt["before_absent"] and before!=b"")):
            raise ConceptGroupError("Collection recovery states changed; preserve them for review.")
        _validate(_json(after))
        current=self._read()
        if current==after: outcome="committed"
        elif current==(None if receipt["before_absent"] else before): outcome="unchanged"
        else: raise ConceptGroupError("Collection state differs from both recovery states; it was left unchanged.")
        if receipt["schema_version"]==2:
            history.resolve_native(receipt,before,after,current,outcome)
        self._write_bytes(recovery/"result.json",_bytes({"operation_id":receipt["operation_id"],"outcome":outcome}))
        if self._read(self.pending,4096)!=raw: raise ConceptGroupError("Pending collection recovery changed.")
        _assert_unredirected(self.pending); self.pending.unlink()
        return outcome

    def _change(self,group_id,expected,change,*,history_adapter=None,restored_from=None,proof_guard=None):
        history=history_adapter if history_adapter is not None else self._history()
        if history is not None: history.require_path(self.path)
        with self._history_guard(history):
            with self._writer():
                value,revision=self.snapshot()
                if revision!=expected: raise ConceptGroupError("Training collections changed. Reload and retry.")
                if group_id is not None and group_id not in value["groups"]:
                    raise ConceptGroupError("This collection no longer exists. Reload the list.")
                with proof_guard() if proof_guard else nullcontext():
                    group=change(value)
                    if history is None: self._commit(value,revision)
                    else: self._commit(value,revision,history=history,group_id=group["id"],restored_from=restored_from)
                    return deepcopy(group)

    def create(self,name,description="",*,expected_revision):
        name=_text(name,120,required=True); description=_text(description,4000)
        def change(value):
            now=datetime.now(timezone.utc).isoformat()
            group={"id":"concept."+uuid4().hex,"name":name,"description":description,
                   "members":[],"created_at":now,"updated_at":now}
            value["groups"][group["id"]]=group
            return group
        return self._change(None,expected_revision,change)

    def edit(self,group_id,name,description,*,expected_revision):
        def change(value):
            group=value["groups"][group_id]
            group.update(name=_text(name,120,required=True),description=_text(description,4000),updated_at=datetime.now(timezone.utc).isoformat())
            return group
        return self._change(group_id,expected_revision,change)

    def add_members(self,group_id,members,*,expected_revision):
        if len(members)>MAX_ADD: raise ConceptGroupError("Add up to 5,000 selected images at a time.")
        rows=[_member(row) for row in members]
        def change(value):
            group=value["groups"][group_id]; current={_key(row):row for row in group["members"]}
            ignored={_key(row):row for row in group.get("ignored_members",[])}
            restored={_key(row) for row in rows}
            for row in rows:
                key=_key(row)
                if key in current and current[key]["image_sha256"]!=row["image_sha256"]:
                    raise ConceptGroupError("A member image changed. Review and remove its old membership before adding its replacement.")
                explicit={**deepcopy(current.get(key,ignored.get(key,{}))),**deepcopy(row)}
                explicit.pop("membership_source",None)
                current[key]=explicit
            if "ignored_members" in group:
                group["ignored_members"]=[row for row in group["ignored_members"] if _key(row) not in restored]
            group["members"]=list(current.values()); group["updated_at"]=datetime.now(timezone.utc).isoformat()
            return group
        return self._change(group_id,expected_revision,change)

    def remove_members(self,group_id,members,*,expected_revision):
        selected={_key(_member(row)) for row in members}
        def change(value):
            group=value["groups"][group_id]
            ignored={_key(row):row for row in group.get("ignored_members",[])}
            for row in group["members"]:
                if _key(row) in selected:
                    ignored[_key(row)]=deepcopy(row)
            group["members"]=[row for row in group["members"] if _key(row) not in selected]
            group["ignored_members"]=list(ignored.values())
            value["schema_version"]=2
            group["updated_at"]=datetime.now(timezone.utc).isoformat()
            return group
        return self._change(group_id,expected_revision,change)

    def restore_members(self,group_id,members,*,expected_revision):
        """An explicit restore becomes manual membership, even if rules changed."""
        value,revision=self.snapshot()
        if revision!=expected_revision:
            raise ConceptGroupError("Training collections changed. Reload and retry.")
        group=value["groups"].get(group_id)
        if group is None:
            raise ConceptGroupError("This collection no longer exists. Reload the list.")
        ignored={_key(row):row for row in group.get("ignored_members",[])}
        rows=[_member(row) for row in members]
        for row in rows:
            original=ignored.get(_key(row))
            if original is None or original["image_sha256"]!=row["image_sha256"]:
                raise ConceptGroupError("An ignored image changed. Review and explicitly add its replacement from Dataset.")
        return self.add_members(group_id,rows,expected_revision=expected_revision)

    def disable_rules(self,group_id,*,expected_revision):
        """Keep the current images and ignored list when automatic matching stops."""
        def change(value):
            group=value["groups"][group_id]
            if group.get("rules") is None:
                return group
            group.pop("rules",None)
            for row in group["members"]:
                row.pop("membership_source",None)
            group["updated_at"]=datetime.now(timezone.utc).isoformat()
            return group
        return self._change(group_id,expected_revision,change)

    def apply_rule_update(self,prepared,dataset,*,expected_revision):
        """Publish a complete, still-current rule preview through normal recovery."""
        def change(value):
            group=value["groups"][prepared.group_id]
            updated=_rule_group_update(group,prepared,dataset)
            if updated==group:
                return group
            group.update(updated,updated_at=datetime.now(timezone.utc).isoformat())
            value["schema_version"]=2
            return group
        return self._change(prepared.group_id,expected_revision,change)


@dataclass(frozen=True)
class PreparedCollectionRules:
    group_id: str
    group_fingerprint: str
    preview: object
    members: tuple[dict,...]
    capture_fingerprint: str


def _rule_group_update(group,prepared,dataset):
    """The same verified rule transformation for ordinary saves and history."""
    from .collection_rules import normalize_collection_rules,rule_preview_is_current
    if prepared.capture_fingerprint!=_rule_capture_fingerprint(prepared.preview,prepared.members):
        raise ConceptGroupError("Collection preview changed after image checks. Preview again.")
    rules=normalize_collection_rules(prepared.preview.rules)
    if prepared.preview.unavailable or not rule_preview_is_current(prepared.preview,dataset):
        raise ConceptGroupError("Collection rule evidence changed or is incomplete. Preview again.")
    if group.get("id")!=prepared.group_id or group_fingerprint(group)!=prepared.group_fingerprint:
        raise ConceptGroupError("Collection changed after the preview. Preview again.")
    ignored={_key(row) for row in group.get("ignored_members",[])}
    current={_key(row):row for row in group["members"]}
    manual={key:row for key,row in current.items() if row.get("membership_source","manual")=="manual"}
    automatic={}
    for raw in prepared.members:
        row=_member(raw); key=_key(row)
        if key in ignored or key in manual: continue
        if key in current and current[key]["image_sha256"]!=row["image_sha256"]:
            raise ConceptGroupError("An automatic member image changed. Remove it and explicitly restore its replacement.")
        automatic[key]={**deepcopy(current.get(key,{})),**row,"membership_source":"automatic"}
    result=deepcopy(group)
    result.update(rules=deepcopy(rules),members=[*manual.values(),*(automatic[key] for key in sorted(automatic))])
    return result


def _rule_capture_fingerprint(preview,members):
    return _hash(_bytes({"rules":preview.rules,"paths":[str(path) for path in preview.paths],"members":members}))


def prepare_rule_update(group,rules,dataset,*,progress=None,should_cancel=None):
    """Match cached knowledge, then hash exact images off the interface thread."""
    from .collection_rules import preview_collection_rules,rule_preview_is_current
    frozen=deepcopy(group)
    preview=preview_collection_rules(rules,dataset,progress=progress,should_cancel=should_cancel)
    if preview.unavailable:
        raise ConceptGroupError(
            "Collection rules need current, available library evidence; existing membership was kept. "
            +"; ".join(str(item) for item in preview.unavailable[:5])
        )
    ignored={_key(row) for row in frozen.get("ignored_members",[])}
    manual={_key(row) for row in frozen["members"] if row.get("membership_source","manual")=="manual"}
    paths=[]
    for path in preview.paths:
        identity=dataset.identity_for_path(path)
        if identity is None:
            raise ConceptGroupError("A matching image lost its library identity. Preview again.")
        key=(identity.library_id.casefold(),identity.relative_path.casefold())
        if key not in ignored and key not in manual:
            paths.append(path)
    if len(paths)+len(manual)+len(ignored)>MAX_MEMBERS:
        raise ConceptGroupError("Collection membership exceeds the supported collection limit.")
    members=[]
    for start in range(0,len(paths),MAX_ADD):
        if should_cancel and should_cancel():
            raise InterruptedError("Collection rule preparation was cancelled.")
        members.extend(capture_members(paths[start:start+MAX_ADD],dataset,
                                       progress=progress,should_cancel=should_cancel))
    if not rule_preview_is_current(preview,dataset):
        raise ConceptGroupError("Collection rule evidence changed during image checks. Preview again.")
    captured=tuple(members)
    return PreparedCollectionRules(str(frozen["id"]),group_fingerprint(frozen),preview,captured,
                                   _rule_capture_fingerprint(preview,captured))


def _image_hash(path,should_cancel=None):
    path=Path(path).absolute(); _assert_unredirected(path)
    from .library_storage import LibraryStorageInspector
    LibraryStorageInspector().assert_locally_readable(path)
    fd=os.open(path,os.O_RDONLY|getattr(os,"O_BINARY",0)|getattr(os,"O_NOFOLLOW",0))
    with os.fdopen(fd,"rb") as handle:
        before=os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size>512*1024*1024:
            raise ConceptGroupError("Collection member is not a supported regular image file (maximum 512 MiB).")
        digest=hashlib.sha256()
        while True:
            if should_cancel and should_cancel(): raise ConceptGroupError("Collection image check cancelled; membership was not changed.")
            block=handle.read(1024*1024)
            if not block: break
            digest.update(block)
        if _identity(before)!=_identity(os.fstat(handle.fileno())) or _identity(before)!=_identity(path.stat(follow_symlinks=False)):
            raise ConceptGroupError("Collection image changed during inspection.")
    _assert_unredirected(path)
    return digest.hexdigest()


def capture_members(paths,dataset,*,progress=None,should_cancel=None):
    paths=tuple(dict.fromkeys(Path(p).absolute() for p in paths))
    if len(paths)>MAX_ADD: raise ConceptGroupError("Add up to 5,000 selected images at a time.")
    result=[]
    for index,path in enumerate(paths,1):
        identity=dataset.identity_for_path(path)
        if identity is None or Path(dataset.path_for_identity(identity)).absolute()!=path:
            raise ConceptGroupError("Select images in a currently configured named library.")
        row={"library_id":identity.library_id,"relative_path":identity.relative_path,"image_sha256":_image_hash(path,should_cancel)}
        if dataset.identity_for_path(path)!=identity: raise ConceptGroupError("Library identity changed during inspection.")
        result.append(_member(row))
        if progress: progress(index,len(paths),path.name)
    return result


def resolve_members(group,dataset,*,progress=None,should_cancel=None):
    paths=[]; identities={}; issues=[]
    for index,value in enumerate(group["members"],1):
        row=_member(value); identity=LibraryImageIdentity(row["library_id"],row["relative_path"])
        try:
            path=Path(dataset.path_for_identity(identity)).absolute()
            if dataset.identity_for_path(path)!=identity: raise ConceptGroupError("Library membership changed.")
            if _image_hash(path,should_cancel)!=row["image_sha256"]: raise ConceptGroupError("Image bytes changed; review its membership.")
            paths.append(path); identities[path]=identity
        except (OSError,KeyError,ValueError,RuntimeError) as error:
            if should_cancel and should_cancel(): raise ConceptGroupError("Collection resolution cancelled.") from error
            issues.append(f"{row['relative_path']}: {error}")
        if progress: progress(index,len(group["members"]),row["relative_path"])
    return {"paths":tuple(paths),"identities":identities,"issues":tuple(issues)}


def verify_plan_membership(group,plan):
    """Prevent replacement bytes between concept resolution and planner reads."""
    if plan.cancelled: return
    expected={_key(_member(row)):row["image_sha256"] for row in group["members"]}
    actual={}
    for candidate in plan.candidates:
        source=candidate.source_identity
        key=(str(source.get("library_id","")).casefold(),str(source.get("relative_image_path","")).casefold())
        if key in actual or candidate.evidence is None:
            raise ConceptGroupError("A collection image could not be inspected exactly. Review its membership before planning.")
        actual[key]=candidate.evidence.sha256
    if actual!=expected:
        raise ConceptGroupError("Collection image bytes changed during planning. Review its membership and try again.")
