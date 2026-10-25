"""Qualified, same-image metadata projection without weakening source identity.

Receipts live only in the configured private store. Exact-image admission can
recover a recognized receipt; no directory search or content-based enrollment
is permitted. Multi-frame, undecodable and over-budget media remain held.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

from .history_versions import HistoryError, HistoryEntityRef, _source_token, _reject_reparse, read_record, record_token
from .image_knowledge_history import HistoryWriteInterrupted
from .image_record import _atomic_write_text
from .operation_journal import _canonical_bytes
from .tag_collection import TagCollection
from .verified_transfer import VerifiedTransferEngine

MAX_IMAGE_BYTES = 256 * 1024 * 1024
MAX_PIXELS = 32_000_000
MAX_DECODED_BYTES = 128 * 1024 * 1024
MAX_RECEIPT_BYTES = 16 * 1024 * 1024
_RECEIPT = re.compile(r"projection-([0-9a-f]{32})-([0-9a-f]{64})\Z")


def _preserve_projection_creation_time(image, staged):
    """Freeze Windows creation time before receipt admission/publication.

    NTFS may tunnel the previous destination's creation time into a renamed
    file. Make that value intentional before hashing its identity, so an
    interruption immediately after rename remains exactly recoverable.
    """
    if os.name != "nt":return
    import ctypes
    from ctypes import wintypes
    import msvcrt
    original=image.stat()
    ticks=original.st_birthtime_ns//100+116444736000000000
    creation=wintypes.FILETIME(ticks&0xffffffff,ticks>>32)
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    set_time=kernel.SetFileTime
    set_time.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),ctypes.POINTER(wintypes.FILETIME)]
    set_time.restype=wintypes.BOOL
    create=kernel.CreateFileW
    create.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,
        wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    create.restype=wintypes.HANDLE
    close=kernel.CloseHandle;close.argtypes=[wintypes.HANDLE];close.restype=wintypes.BOOL
    # Read/attribute-write only; share delete for the owned publication. Open
    # the reparse object itself so a replaced pathname cannot redirect writes.
    handle=create(str(staged),0x80000100,7,None,3,0x00200000,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    fd=None
    try:
        fd=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
        held=os.fstat(fd);located=staged.stat()
        signature=lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
        if (getattr(held,"st_file_attributes",0)&0x400 or signature(held)!=signature(located)):
            raise HistoryError("Projection candidate changed before creation-time preservation.")
        if not set_time(handle,ctypes.byref(creation),None,None):
            raise ctypes.WinError(ctypes.get_last_error())
        after=os.fstat(fd)
        if signature(after)!=signature(held) or staged.stat().st_ino!=held.st_ino:
            raise HistoryError("Projection candidate changed while preserving creation time.")
        if after.st_birthtime_ns!=original.st_birthtime_ns:
            raise HistoryError("Projection creation time failed exact read-back.")
    finally:
        if fd is not None:os.close(fd)
        else:close(handle)


def _digest(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _cancel(cancelled):
    if cancelled and cancelled():
        raise InterruptedError("Metadata projection cancelled before publication.")


def _file_digest(path, cancelled=None):
    _reject_reparse(path)
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise HistoryError("Metadata identity proof exceeds the 256 MiB image limit.")
    digest=hashlib.sha256(); count=0
    with path.open("rb") as stream:
        while chunk:=stream.read(1024*1024):
            _cancel(cancelled); count+=len(chunk)
            if count>MAX_IMAGE_BYTES:raise HistoryError("Metadata proof image grew beyond its byte limit.")
            digest.update(chunk)
    return digest.hexdigest()


def _pixels(path, cancelled=None):
    """Bound decoded content independently of metadata-reader assertions."""
    _cancel(cancelled)
    from PIL import Image
    try:
        with Image.open(path) as image:
            width,height=image.size
            if (getattr(image,"n_frames",1)!=1 or image.mode not in {"1","L","LA","P","RGB","RGBA"}
                    or width*height>MAX_PIXELS or width*height*4>MAX_DECODED_BYTES):
                raise HistoryError("Metadata identity proof requires a supported single-frame image within 32 MP / 128 MiB decoded limits.")
            image.load(); _cancel(cancelled)
            with image.convert("RGBA") as decoded:
                digest=hashlib.sha256(decoded.tobytes()).hexdigest()
            metadata={key:_info_value(value) for key,value in image.info.items() if key not in {"xmp","XML:com.adobe.xmp"}}
            xmp=image.info.get("xmp",image.info.get("XML:com.adobe.xmp",b""))
            return {"width":width,"height":height,"mode":image.mode,"format":image.format,
                "rgba_sha256":digest,"other_metadata":_digest(metadata),"unmanaged_xmp":_xmp_proof(xmp)}
    except (HistoryError,InterruptedError):raise
    except Exception as exc:
        raise HistoryError("Metadata projection requires a qualified source-identity receipt; decoded-pixel proof is unavailable: "+str(exc)) from exc


def _info_value(value):
    if isinstance(value,bytes):return {"bytes":len(value),"sha256":hashlib.sha256(value).hexdigest()}
    if value is None or type(value) in {str,int,float,bool}:return value
    if isinstance(value,(list,tuple)):return [_info_value(item) for item in value]
    if isinstance(value,dict):return {str(key):_info_value(item) for key,item in value.items()}
    raise HistoryError("Metadata proof encountered an unsupported embedded value.")


def _xmp_proof(raw):
    """Compare all RDF properties except the two explicitly managed fields."""
    if not raw:return _digest([])
    import xml.etree.ElementTree as ET
    if isinstance(raw,str):raw=raw.encode("utf-8")
    if not isinstance(raw,bytes) or len(raw)>8*1024*1024 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise HistoryError("XMP preservation proof exceeds its supported safe bound.")
    root=ET.fromstring(raw)
    rdf="{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
    managed={"{http://purl.org/dc/elements/1.1/}subject","{http://purl.org/dc/elements/1.1/}description"}
    def node(element):
        text=element.text or ""
        if len(element) and not text.strip():text=""
        return [element.tag,sorted(element.attrib.items()),text,
            [node(child) for child in element],element.tail if element.tail and element.tail.strip() else ""]
    properties=[]
    envelopes=[root]
    if root.tag=="{adobe:ns:meta/}xmpmeta":
        if any(child.tag!=rdf+"RDF" for child in root):raise HistoryError("Unsupported XMP envelope; preservation is held.")
        envelopes.extend(root)
    elif root.tag!=rdf+"RDF":raise HistoryError("Unsupported XMP root; preservation is held.")
    for envelope in envelopes:
        for key,value in envelope.attrib.items():
            if key!="{adobe:ns:meta/}xmptk":properties.append(["envelope",envelope.tag,key,value])
        if envelope.tag==rdf+"RDF" and any(child.tag!=rdf+"Description" for child in envelope):
            raise HistoryError("Unsupported XMP RDF statement; preservation is held.")
    descriptions=list(root.iter(rdf+"Description"))
    if not descriptions:raise HistoryError("XMP preservation proof requires recognized RDF descriptions.")
    for description in descriptions:
        subject=description.get(rdf+"about","")
        for key,value in description.attrib.items():
            if key!=rdf+"about" and key not in managed:properties.append([subject,"attribute",key,value])
        for child in description:
            if child.tag not in managed:properties.append([subject,"property",node(child)])
    return _digest(sorted(properties,key=_canonical_bytes))


def _paths(store,image,identifier):
    match=_RECEIPT.fullmatch(identifier)
    if not match:raise HistoryError("Unrecognized metadata projection receipt.")
    transaction=match.group(1)
    receipt=store.scope.history_root/"projection-receipts"/(identifier+".json")
    staged=image.with_name(f".{image.name}.collectigen-projection-{transaction}.tmp")
    parked=image.with_name(f".{image.name}.collectigen-projection-original-{transaction}.tmp")
    for path in (receipt,staged,parked):_reject_reparse(path)
    if not receipt.resolve().is_relative_to(store.scope.history_root):raise HistoryError("Projection receipt escaped private storage.")
    return receipt,staged,parked


def _write_receipt(path,intent,status):
    raw=_canonical_bytes({"schema_version":1,"kind":"metadata_projection","intent":intent,"status":status})
    if len(raw)>MAX_RECEIPT_BYTES:raise HistoryError("Metadata projection receipt exceeds its 16 MiB limit.")
    _reject_reparse(path); path.parent.mkdir(parents=True,exist_ok=True)
    _atomic_write_text(path,raw.decode("utf-8")+"\n")
    if path.read_bytes()!=raw+b"\n":raise HistoryError("Metadata projection receipt failed read-back.")


def _load_receipt(store,image,identifier):
    path,staged,parked=_paths(store,image,identifier)
    with path.open("rb") as stream:raw=stream.read(MAX_RECEIPT_BYTES+1)
    if len(raw)>MAX_RECEIPT_BYTES:raise HistoryError("Metadata projection receipt exceeds its bound.")
    value=json.loads(raw); intent=value.get("intent",{})
    if (value.get("schema_version")!=1 or value.get("kind")!="metadata_projection"
            or value.get("status") not in {"staged","published","cancelled"}
            or _digest(intent)!=_RECEIPT.fullmatch(identifier).group(2)
            or intent.get("scope")!=store.scope.key or str(intent.get("locator","")).casefold()!=image.relative_to(store.scope.library_root).as_posix().casefold()):
        raise HistoryError("Metadata projection receipt authority changed.")
    if staged!=Path(intent.get("staged","")) or parked!=Path(intent.get("parked","")):
        raise HistoryError("Metadata projection staging paths changed.")
    return value,path,staged,parked


def _check_pin(store,intent,image):
    head=store._find(kind="version",entity=intent["entity_id"])
    if (head.get("version_id","") if head else "")!=intent["head"]:
        raise HistoryError("Saved history changed after metadata projection admission.")
    if record_token(read_record(image.with_suffix(".tags.json")))!=intent["record_token"]:
        raise HistoryError("Image Knowledge changed after metadata projection admission.")
    _check_backup(store,intent)
    if intent.get("candidate_backup"):
        _private_candidate(store,Path(intent["candidate_backup"]),intent["candidate_digest"])


def _check_backup(store,intent):
    backup=_private_path(store,Path(intent["backup"]))
    if _file_digest(backup)!=intent["before_digest"]:
        raise HistoryError("The private original-image backup is unavailable or changed.")


def _exact(path,token,digest):
    _reject_reparse(path)
    return path.is_file() and _source_token(path)==token and _file_digest(path)==digest


@dataclass(frozen=True)
class PreparedProjection:
    identifier: str
    intent: dict
    metadata_after: dict


def prepare_projection(context,image,metadata,preview,backup,*,cancelled=None):
    return _prepare_projection(context,image,metadata,preview,backup,cancelled=cancelled)


def _private_candidate(store,path,expected_digest,cancelled=None):
    path=_private_path(store,path)
    if not re.fullmatch(r"[0-9a-f]{64}",str(expected_digest)):
        raise HistoryError("Metadata restore candidate must be an exact verified private backup.")
    if _file_digest(path,cancelled)!=expected_digest:
        raise HistoryError("Metadata restore candidate changed from its admitted digest.")
    return path


def _private_path(store,path):
    path=Path(path).absolute()
    if ".." in path.parts or not path.is_relative_to(store.scope.history_root):
        raise HistoryError("Metadata recovery files must stay inside explicitly configured private history.")
    _reject_reparse(path)
    if not path.resolve().is_relative_to(store.scope.history_root):
        raise HistoryError("Metadata recovery path escaped its private storage.")
    return path


def prepare_projection_from_backup(context,image,metadata,*,candidate_backup,expected_candidate_sha256,current_backup,cancelled=None):
    """Prove and stage exact private backup bytes; never rewrite the candidate."""
    image=Path(image); candidate=Path(candidate_backup)
    with context.locked(image):
        image=context.store.scope.image(image)
        if context.store.has_pending(context.entity):raise HistoryWriteInterrupted("Resolve pending saved data before restoring metadata.")
        candidate=_private_candidate(context.store,candidate,expected_candidate_sha256,cancelled)
        current=metadata.read_embedded(image); restored=metadata.read_embedded(candidate)
        subjects=restored.get("tag_fields",{}).get("Subject",[])
        if isinstance(subjects,str):subjects=[subjects]
        description=str(restored.get("description_fields",{}).get("Description","") or "")
        preview=SimpleNamespace(embedded_standard_metadata=current.get("standard_metadata",{}),
            embedded_generation_fields=current.get("raw_generation_fields",{}),expected_metadata_tags=list(subjects),
            expected_metadata_descriptions=[description] if description else [])
        return _prepare_projection(context,image,metadata,preview,Path(current_backup),cancelled=cancelled,
            candidate_backup=candidate,candidate_digest=expected_candidate_sha256)


def _prepare_projection(context,image,metadata,preview,backup,*,cancelled=None,candidate_backup=None,candidate_digest=""):
    """Prove a staged candidate while the original image is still untouched."""
    image=Path(image); store=context.store
    with context.locked(image):
        image=store.scope.image(image)
        if store.has_pending(context.entity):raise HistoryWriteInterrupted("Resolve pending saved data before projecting metadata.")
        head=store.current_head(context.entity)
        before_head=head.version_id if head else ""
        before_record=record_token(read_record(image.with_suffix(".tags.json")))
        before_digest=_file_digest(image,cancelled); pixels=_pixels(image,cancelled)
        before=metadata.read_embedded(image)
        if (before.get("standard_metadata",{})!=preview.embedded_standard_metadata
                or before.get("raw_generation_fields",{})!=preview.embedded_generation_fields):
            raise HistoryError("Embedded evidence changed since metadata preview.")
        backup=_private_path(store,Path(backup))
        if _file_digest(backup,cancelled)!=before_digest:
            raise HistoryError("Metadata publication requires its verified private original-image backup.")
        transaction=uuid4().hex
        # Digest is added only after all immutable proof fields are frozen.
        provisional="projection-"+transaction+"-"+"0"*64
        _,staged,parked=_paths(store,image,provisional)
        if staged.exists() or parked.exists():raise HistoryError("Metadata projection staging is occupied.")
        copy_source=candidate_backup if candidate_backup is not None else image
        copy_digest=candidate_digest if candidate_backup is not None else before_digest
        copied_identity=VerifiedTransferEngine()._copy_verified(copy_source,staged,copy_digest)
        if _digest(copied_identity)!=_source_token(staged):raise HistoryError("Projection copy staging was replaced after creation.")
        _cancel(cancelled)
        if candidate_backup is None:
            metadata.write_xmp(staged,preview.expected_metadata_tags,"\n\n".join(preview.expected_metadata_descriptions))
        _preserve_projection_creation_time(image,staged)
        candidate_token=_source_token(staged)
        after_digest=_file_digest(staged,cancelled); after=metadata.read_embedded(staged)
        if candidate_backup is not None:
            _private_candidate(store,candidate_backup,candidate_digest,cancelled)
            if after_digest!=candidate_digest:raise HistoryError("Staged metadata restore no longer matches the exact backup bytes.")
        if _pixels(staged,cancelled)!=pixels:raise HistoryError("Metadata writer changed decoded image pixels; original retained.")
        if (after.get("standard_metadata",{})!=before.get("standard_metadata",{})
                or after.get("raw_generation_fields",{})!=before.get("raw_generation_fields",{})):
            raise HistoryError("Metadata writer changed canonical Metadata or exact generator evidence; original retained.")
        subjects=after.get("tag_fields",{}).get("Subject",[])
        if isinstance(subjects,str):subjects=[subjects]
        if TagCollection(subjects).normalized_set()!=TagCollection(preview.expected_metadata_tags).normalized_set():
            raise HistoryError("Staged metadata Tags failed exact managed-field verification.")
        expected="\n\n".join(preview.expected_metadata_descriptions)
        if str(after.get("description_fields",{}).get("Description","") or "")!=expected:
            raise HistoryError("Staged metadata Description failed exact read-back verification.")
        if _source_token(staged)!=candidate_token or _source_token(image)!=context.entity.source_token or _file_digest(image,cancelled)!=before_digest:
            raise HistoryError("A source or staged instance changed while proving metadata preservation.")
        head=store.current_head(context.entity)
        if (head.version_id if head else "")!=before_head or record_token(read_record(image.with_suffix(".tags.json")))!=before_record:
            raise HistoryError("Saved history or Image Knowledge changed while proving metadata preservation.")
        intent={"scope":store.scope.key,"entity_id":context.entity.entity_id,"locator":context.entity.locator,
            "transaction":transaction,"old_token":context.entity.source_token,"new_token":candidate_token,
            "before_digest":before_digest,"after_digest":after_digest,"backup":str(backup),
            "record_token":before_record,"head":before_head,
            "staged":str(staged),"parked":str(parked),"pixels":pixels,
            "standard_metadata":after.get("standard_metadata",{}),"raw_generation_fields":after.get("raw_generation_fields",{}),
            "managed_tags":list(preview.expected_metadata_tags),"managed_description":expected,
            "candidate_backup":str(candidate_backup) if candidate_backup is not None else "","candidate_digest":candidate_digest}
        identifier="projection-"+transaction+"-"+_digest(intent)
        path,_,_=_paths(store,image,identifier)
        _write_receipt(path,intent,"staged")
        return PreparedProjection(identifier,intent,after)


def publish_projection(context,image,prepared,*,cancelled=None):
    image=Path(image); store=context.store
    with context.locked(image):
        image=store.scope.image(image)
        value,path,staged,parked=_load_receipt(store,image,prepared.identifier)
        intent=value["intent"]
        if intent!=prepared.intent or value["status"]!="staged":raise HistoryError("Projection admission no longer matches its receipt.")
        _check_pin(store,intent,image)
        if not _exact(image,intent["old_token"],intent["before_digest"]) or not _exact(staged,intent["new_token"],intent["after_digest"]):
            raise HistoryError("A metadata projection instance was replaced before publication.")
        if parked.exists():raise HistoryError("Metadata original-instance parking is occupied.")
        _cancel(cancelled)
        store.transition_binding(intent["entity_id"],intent["locator"],intent["old_token"],state="moving",receipt_id=prepared.identifier)
        try:
            VerifiedTransferEngine._rename_no_replace(image,parked)
            if not _exact(parked,intent["old_token"],intent["before_digest"]):raise HistoryError("Original image instance changed during publication.")
            if not _exact(staged,intent["new_token"],intent["after_digest"]):raise HistoryError("Staged image instance changed during publication.")
            VerifiedTransferEngine._rename_no_replace(staged,image)
            if not _exact(image,intent["new_token"],intent["after_digest"]):raise HistoryError("Published image instance changed.")
            _write_receipt(path,intent,"published")
            # Retain the private verified original; discard only its exact
            # parked instance after a successful publication and receipt.
            if not _exact(parked,intent["old_token"],intent["before_digest"]):raise HistoryError("Parked original changed before cleanup.")
            _check_backup(store,intent)
            parked.unlink()
            store.transition_binding(intent["entity_id"],intent["locator"],intent["new_token"],state="active",receipt_id=prepared.identifier)
        except Exception as exc:
            raise HistoryWriteInterrupted("Metadata publication needs exact receipt recovery; all current files were retained: "+str(exc)) from exc
        return replace(context,entity=HistoryEntityRef(store.scope.key,intent["entity_id"],intent["locator"],intent["new_token"]))


def validate_context_renewal(previous,renewed,image):
    """Validate explicit replacement of a cached immutable writer context."""
    if (previous.store is not renewed.store or previous.authority_guard is not renewed.authority_guard
            or replace(previous,entity=renewed.entity)!=renewed
            or (previous.entity.scope_key,previous.entity.entity_id,previous.entity.locator)
            !=(renewed.entity.scope_key,renewed.entity.entity_id,renewed.entity.locator)):
        raise HistoryError("Projection context renewal changed its operation or image authority.")
    with renewed.locked(image):
        binding=renewed.store.binding(entity_id=renewed.entity.entity_id)
        value,_,_,_=_load_receipt(renewed.store,Path(image),binding.get("receipt_id",""))
        intent=value["intent"]
        if (value["status"]!="published" or intent["old_token"]!=previous.entity.source_token
                or intent["new_token"]!=renewed.entity.source_token):
            raise HistoryError("Projection context renewal lacks its exact published receipt.")


def recover_projection_before_bind(store,image):
    """Caller holds its configuration guard; touch only this exact receipt."""
    image=store.scope.image(image); locator=image.relative_to(store.scope.library_root).as_posix()
    with store.locked(image):
        store._ready(); binding=store.binding(locator=locator)
        if not binding or binding.get("binding_state")!="moving":return
        identifier=binding.get("receipt_id","")
        if not _RECEIPT.fullmatch(identifier):return
        value,path,staged,parked=_load_receipt(store,image,identifier); intent=value["intent"]
        if binding.get("entity_id")!=intent["entity_id"] or binding.get("source_token")!=intent["old_token"]:
            raise HistoryError("Projection recovery binding differs from its receipt.")
        _check_pin(store,intent,image)
        if image.exists():
            if _exact(image,intent["new_token"],intent["after_digest"]):
                state="published"; token=intent["new_token"]
            elif _exact(image,intent["old_token"],intent["before_digest"]) and not parked.exists():
                state="cancelled"; token=intent["old_token"]
            else:raise HistoryError("A foreign current image blocks metadata recovery; retain every instance.")
        elif _exact(parked,intent["old_token"],intent["before_digest"]):
            VerifiedTransferEngine._rename_no_replace(parked,image)
            state="cancelled"; token=intent["old_token"]
        else:raise HistoryError("Exact original/published image instance is unavailable for metadata recovery.")
        # Unknown staging never becomes cleanup authority merely through hashes.
        for candidate,expected_token,digest in ((staged,intent["new_token"],intent["after_digest"]),(parked,intent["old_token"],intent["before_digest"])):
            if candidate.exists() and not _exact(candidate,expected_token,digest):
                raise HistoryError("A staging instance was replaced; metadata recovery is held.")
        _write_receipt(path,intent,state)
        for candidate,expected_token,digest in ((staged,intent["new_token"],intent["after_digest"]),(parked,intent["old_token"],intent["before_digest"])):
            if candidate.exists():
                if not _exact(candidate,expected_token,digest):raise HistoryError("Projection cleanup instance changed.")
                _check_backup(store,intent)
                candidate.unlink()
        store.transition_binding(intent["entity_id"],locator,token,state="active",receipt_id=identifier)
