from __future__ import annotations
import json
import os
import shutil
import subprocess
import csv
import re
import queue
import threading
import time
import atexit
import sys
import weakref
from pathlib import Path
from types import SimpleNamespace
from .caption_import import (
    has_broken_metadata_quotes, looks_like_clear_tag,
    looks_like_metadata_prose_fragment,
)
from .tag_collection import TagCollection
from .generation_metadata import parse_generation_payload

_NO_CONSOLE={"creationflags":getattr(subprocess,"CREATE_NO_WINDOW",0)}
_UNCHANGED=object()


def _is_xml_10_character(value: str) -> bool:
    code=ord(value)
    return (
        code in (0x09,0x0A,0x0D)
        or 0x20<=code<=0xD7FF
        or 0xE000<=code<=0xFFFD
        or 0x10000<=code<=0x10FFFF
    )


def portable_xmp_text(value: object) -> str:
    """Return a deterministic XML 1.0 representation for app-managed XMP."""
    result=[]
    for character in "" if value is None else str(value):
        if _is_xml_10_character(character):
            result.append(character)
        elif ord(character)<0x20:
            result.append(" ")
        else:
            result.append("\N{REPLACEMENT CHARACTER}")
    return "".join(result)


def xmp_incompatible_character_count(value: object) -> int:
    return sum(
        not _is_xml_10_character(character)
        for character in ("" if value is None else str(value))
    )


def _exiftool_cstr_argument(value: object) -> str:
    escaped=(
        str(value)
        .replace("\\","\\\\")
        .replace("\n","\\n")
        .replace("\r","\\r")
        .replace("\t","\\t")
    )
    return "#[CSTR]"+escaped


def _run_exiftool_argfile(
    exiftool: str,arguments,*,timeout: float=120.0,
) -> None:
    payload=(
        "\n".join(_exiftool_cstr_argument(value) for value in arguments)+"\n"
    ).encode("utf-8")
    timeout=max(0.05,float(timeout))
    try:
        subprocess.run(
            [exiftool,"-@","-"],input=payload,check=True,capture_output=True,
            timeout=timeout,**_NO_CONSOLE,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"ExifTool did not finish the Metadata write within {timeout:g} seconds."
        ) from exc


# Active read sessions belong to this process; writes use separate bounded calls.
_ACTIVE_METADATA_READERS=set()
_METADATA_READERS_LOCK=threading.RLock()
_METADATA_SHUTDOWN_ERRORS=()


def shutdown_metadata_readers(*,timeout: float=2.0,force: bool=False) -> bool:
    """Drain idle readers after jobs, or cancel owned reads during process exit."""
    global _METADATA_SHUTDOWN_ERRORS
    deadline=time.monotonic()+max(0.05,float(timeout)); errors=[]
    with _METADATA_READERS_LOCK: readers=tuple(_ACTIVE_METADATA_READERS)
    for reader in readers:
        remaining=deadline-time.monotonic()
        if remaining<=0:
            errors.append("Metadata reader shutdown exceeded its time budget."); break
        # Even exit cancellation first offers the read process graceful EOF.
        # close escalates to killing this owned process only if it cannot drain.
        try: reader.close(timeout=remaining,cancel_active=force)
        except (OSError,RuntimeError,TimeoutError) as exc: errors.append(str(exc))
    _METADATA_SHUTDOWN_ERRORS=tuple(errors)
    return not errors


def _close_metadata_readers_at_exit():
    if not shutdown_metadata_readers(timeout=5.0,force=True):
        sys.stderr.write("Metadata read-session shutdown remains incomplete.\n")


atexit.register(_close_metadata_readers_at_exit)


class _PersistentExifToolReader:
    """Serialized read commands with explicit process and output-thread ownership."""

    def __init__(self,exiftool: str,*,timeout: float=120.0) -> None:
        self.exiftool=str(exiftool); self.timeout=max(0.05,float(timeout))
        self._process=None; self._lines=queue.Queue(); self._lock=threading.RLock()
        self._sequence=0; self._output_thread=None; self._closing=threading.Event()
        self._shutdown_writer=None; self._shutdown_writer_process=None
        self._shutdown_writer_errors=[]

    @staticmethod
    def _write_command(stream,payload: bytes) -> None:
        # Unbuffered pipes can write fewer bytes than requested.
        pending=memoryview(payload)
        while pending:
            count=stream.write(pending)
            if not isinstance(count,int) or count<=0:
                raise BrokenPipeError("ExifTool command pipe stopped accepting bytes.")
            pending=pending[count:]
        stream.flush()

    def _start(self) -> None:
        if self._closing.is_set():raise InterruptedError("Metadata reader is closing.")
        process=self._process
        if process is not None and process.poll() is None:return
        if process is not None:self.close(force=True)
        process=subprocess.Popen(
            [self.exiftool,"-stay_open","True","-@","-"],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            bufsize=0,**_NO_CONSOLE,
        )
        if process.stdin is None or process.stdout is None:
            process.kill(); process.wait(timeout=2)
            raise RuntimeError("ExifTool did not open its command pipes.")
        lines=queue.Queue(); self._process=process; self._lines=lines
        # Capture this session's queue: an old EOF cannot enter a successor.
        def read_output():
            pending=b""
            try:
                while True:
                    chunk=process.stdout.read(65536)
                    if not chunk:break
                    pending+=chunk
                    while b"\n" in pending:
                        line,pending=pending.split(b"\n",1); lines.put(line+b"\n")
                if pending:lines.put(pending)
            except Exception as exc:lines.put(exc)
            finally:lines.put(None)
        self._output_thread=threading.Thread(
            target=read_output,daemon=True,name="CollectiGen Metadata output",
        )
        with _METADATA_READERS_LOCK:_ACTIVE_METADATA_READERS.add(self)
        try:self._output_thread.start()
        except RuntimeError as start_error:
            # A failed thread start owns no stdout lock. Do not join an
            # unstarted thread; retain the process until bounded cleanup ends.
            if self._output_thread.ident is None:self._output_thread=None
            try:self.close(force=True,timeout=min(2.0,self.timeout))
            except (OSError,RuntimeError,TimeoutError) as cleanup_error:
                raise RuntimeError(
                    f"Metadata output reader failed to start ({start_error}); "
                    f"owned session cleanup remains incomplete ({cleanup_error})."
                ) from start_error
            raise

    def execute(self,args) -> bytes:
        if self._closing.is_set():raise InterruptedError("Metadata reader is closing.")
        with self._lock:
            if self._closing.is_set():raise InterruptedError("Metadata reader is closing.")
            self._start(); process=self._process
            if process is None or process.stdin is None:raise RuntimeError("ExifTool is not running.")
            self._sequence+=1; token=f"{self._sequence:08d}"
            lines=["-charset","filename=utf8","-q","-q",*(str(value) for value in args),f"-execute{token}"]
            payload=("\n".join(lines)+"\n").encode("utf-8")
            try:self._write_command(process.stdin,payload)
            except Exception:
                if self._closing.is_set():raise InterruptedError("Metadata read cancelled by reader shutdown.")
                self.close(force=True); raise
            output=[]; marker=f"{{ready{token}}}".encode("ascii")
            deadline=time.monotonic()+self.timeout
            while True:
                if self._closing.is_set():raise InterruptedError("Metadata read cancelled by reader shutdown.")
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    self.close(force=True); raise TimeoutError("ExifTool did not finish the Metadata batch in time.")
                try:line=self._lines.get(timeout=min(remaining,0.1))
                except queue.Empty:continue
                if line is None or isinstance(line,Exception):
                    if self._closing.is_set():raise InterruptedError("Metadata read cancelled by reader shutdown.")
                    self.close(force=True)
                    raise RuntimeError("ExifTool stopped before the Metadata batch completed.") from (line if isinstance(line,Exception) else None)
                if line.strip()==marker:return b"".join(output)
                output.append(line)

    def close(self,*,force: bool=False,timeout: float=2.0,cancel_active: bool=True) -> None:
        deadline=time.monotonic()+max(0.05,float(timeout))
        remaining=lambda:max(0.0,deadline-time.monotonic())
        if cancel_active:self._closing.set()
        acquired=self._lock.acquire(timeout=min(0.1,remaining())) if cancel_active else self._lock.acquire(blocking=False)
        if not acquired:
            if not cancel_active:raise TimeoutError("A Metadata read is still active; its session was retained.")
            # Only this read session's owned process: unblock a stalled pipe write.
            process=self._process
            if process is not None and process.poll() is None:process.kill()
            acquired=self._lock.acquire(timeout=remaining())
            if not acquired:raise TimeoutError("Metadata read cancellation has not drained yet.")
        self._closing.set(); completed=False
        try:
            process=self._process; output_thread=self._output_thread
            writer=self._shutdown_writer
            if writer is not None and self._shutdown_writer_process is not process:
                raise RuntimeError("Metadata shutdown writer belongs to another retained session.")
            if process is None:
                completed=True; return
            if not force and process.poll() is None and process.stdin is not None:
                # Reserve part of the overall deadline for stop/reap/join. A
                # graceful phase has at most the previous 0.5s allowance; stdin
                # write AND flush now share it instead of blocking the caller.
                grace_deadline=time.monotonic()+min(0.5,remaining()/2)
                if writer is None:
                    errors=[]; stream=process.stdin
                    def write_shutdown():
                        try:self._write_command(stream,b"-stay_open\nFalse\n")
                        except BaseException as exc:errors.append(exc)
                    writer=threading.Thread(target=write_shutdown,daemon=True,
                                            name="CollectiGen Metadata shutdown command")
                    self._shutdown_writer=writer
                    self._shutdown_writer_process=process
                    self._shutdown_writer_errors=errors
                    try:writer.start()
                    except RuntimeError:
                        # A failed native thread start has no writer to join.
                        # Keep the process/session for an explicit forced retry.
                        if writer.ident is None:
                            self._shutdown_writer=None; self._shutdown_writer_process=None
                            self._shutdown_writer_errors=[]
                        raise
                writer.join(timeout=min(remaining(),max(0,grace_deadline-time.monotonic())))
                if writer.is_alive() or self._shutdown_writer_errors:
                    force=True
                else:
                    try:process.wait(timeout=min(remaining(),max(0,grace_deadline-time.monotonic())))
                    except subprocess.TimeoutExpired:force=True
            if process.poll() is None:
                process.kill(); process.wait(timeout=remaining())
            # Both workers can own pipe locks. Retain every handle until both
            # have drained; retry reuses this exact writer, never starts another.
            if writer is not None:
                writer.join(timeout=remaining())
                if writer.is_alive():
                    raise TimeoutError("Metadata command writer has not drained; its session and pipes were retained.")
            if output_thread is not None:
                output_thread.join(timeout=remaining())
                if output_thread.is_alive():raise TimeoutError("Metadata output thread has not drained; pipes were retained.")
            if process.stdin is not None:process.stdin.close()
            if process.stdout is not None:process.stdout.close()
            errors=self._shutdown_writer_errors
            self._process=None; self._output_thread=None
            self._shutdown_writer=None; self._shutdown_writer_process=None
            self._shutdown_writer_errors=[]; completed=True
            with _METADATA_READERS_LOCK:_ACTIVE_METADATA_READERS.discard(self)
            unexpected=next((error for error in errors if not isinstance(error,OSError)),None)
            if unexpected is not None:
                raise RuntimeError("Metadata shutdown command failed after its session was drained.") from unexpected
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Owned Metadata process has not stopped; its session was retained.") from exc
        finally:
            if completed:self._closing.clear()
            self._lock.release()

class MetadataSyncService:
    READER_VERSION=1
    _READ_FIELDS=(
        "-XMP:Subject","-XMP:HierarchicalSubject","-IPTC:Keywords","-EXIF:XPKeywords",
        "-XMP:Description","-IPTC:Caption-Abstract","-EXIF:ImageDescription",
        "-PNG:Parameters","-PNG:Comment","-PNG:Prompt","-PNG:Workflow",
        "-PNG:Software","-EXIF:Software","-XMP:CreatorTool","-ImageWidth","-ImageHeight",
        "-FileType","-MIMEType","-Orientation","-ColorSpace","-ColorType",
        "-BitsPerSample","-ProfileDescription","-Error",
    )
    def __init__(
        self,exiftool: str|None=None,*,knowledge=None,characters=None,
        persistent_reads: bool=True,read_timeout: float=120.0,
        write_timeout: float|None=None,
    ):
        if not exiftool:
            from .dependencies import DependencyService

            exiftool = DependencyService().exiftool_path()
        self.exiftool=exiftool or ""
        self.knowledge=knowledge
        self.characters=characters
        self.persistent_reads=bool(persistent_reads)
        self.read_timeout=max(0.05,float(read_timeout))
        self.write_timeout=max(
            0.05,float(read_timeout if write_timeout is None else write_timeout),
        )
        self._reader=None; self._reader_finalizer=None

    def close(self,*,force: bool=False) -> None:
        reader=self._reader
        if reader is not None:
            if force:reader.close(force=True)
            else:reader.close()
            self._reader=None
        finalizer=getattr(self,"_reader_finalizer",None)
        if finalizer is not None:finalizer.detach(); self._reader_finalizer=None
    def is_available(self) -> bool:
        return bool(self.exiftool) and (Path(self.exiftool).is_file() or shutil.which(self.exiftool) is not None)
    def write_xmp(self,image:Path,tags:list[str],description:str|None=None):
        self.write_managed_xmp(
            image,tags=tags,
            **({"description":description} if description is not None else {}),
        )

    def write_managed_xmp(
        self,image: Path,*,tags=_UNCHANGED,description=_UNCHANGED,
    ) -> None:
        """Replace only the explicitly supplied app-managed XMP fields."""

        if tags is _UNCHANGED and description is _UNCHANGED:
            return
        managed_tags=(
            [portable_xmp_text(tag) for tag in tags]
            if tags is not _UNCHANGED else None
        )
        managed_description=(
            portable_xmp_text(description)
            if description is not _UNCHANGED else None
        )
        base=[
            '-charset','filename=utf8','-charset','exiftool=utf8',
            '-overwrite_original',
        ]
        # ExifTool evaluates replacement/addition assignments for list-valued
        # PNG XMP subjects against the original list even when '=' appears
        # first. Clearing in its own invocation is the only reliable reset.
        if managed_tags is not None:
            _run_exiftool_argfile(
                self.exiftool,[*base,'-XMP:Subject=',str(image)],
                timeout=self.write_timeout,
            )
        cmd=list(base)
        if managed_tags:
            cmd.append(f'-XMP:Subject={managed_tags[0]}')
            cmd += [f'-XMP:Subject+={tag}' for tag in managed_tags[1:]]
        if managed_description is not None:
            cmd.append(f'-XMP:Description={managed_description}')
        if len(cmd)>len(base):
            cmd.append(str(image))
            _run_exiftool_argfile(
                self.exiftool,cmd,timeout=self.write_timeout,
            )
    def sync_from_sidecar(self,image:Path):
        from .image_knowledge_reconciliation import ImageKnowledgeReconciliationService

        service = ImageKnowledgeReconciliationService(
            self,
            knowledge=self.knowledge,
            characters=self.characters,
        )
        preview = service.preview(image, include_metadata=True)
        result = service.apply(preview, targets=("txt", "metadata"))
        if result.errors:
            raise RuntimeError("; ".join(result.errors))
        return result

    def read_embedded(self, image: Path) -> dict[str, object]:
        """Read common embedded tag and caption fields without changing the image."""
        path=Path(image)
        value=self.read_embedded_many([path]).get(path)
        if isinstance(value,Exception):
            raise value
        if value is None:
            raise RuntimeError(f"ExifTool returned no Metadata for {path.name}.")
        return value

    def read_embedded_many(
        self,images,*,should_cancel=None,
    ) -> dict[Path,dict[str,object]|Exception]:
        """Read a bounded image group in one ExifTool process."""
        paths=[Path(image) for image in images]
        if not paths:
            return {}
        if not self.is_available():
            error=RuntimeError("Metadata support is unavailable.")
            return {path:error for path in paths}
        arguments=["-j",*self._READ_FIELDS,*(str(path) for path in paths)]
        result=None; persistent_error=None; used_persistent=False
        if self.persistent_reads:
            try:
                if self._reader is None:
                    self._reader=_PersistentExifToolReader(
                        self.exiftool,timeout=self.read_timeout,
                    )
                    self._reader_finalizer=weakref.finalize(self,self._reader.close)
                stdout_value=self._reader.execute(arguments)
                result=SimpleNamespace(stdout=stdout_value,stderr=b"",returncode=0)
                used_persistent=True
            except InterruptedError as exc:
                return {path:exc for path in paths}
            except Exception as exc:
                persistent_error=exc
                try:self.close(force=True)
                except Exception as shutdown_error:
                    return {path:RuntimeError(f"Metadata reader shutdown incomplete: {shutdown_error}") for path in paths}
        def decode(completed):
            stdout=(
                completed.stdout.decode("utf-8","strict")
                if isinstance(completed.stdout,bytes) else str(completed.stdout or "")
            )
            decoded=json.loads(stdout or "[]")
            if not isinstance(decoded,list):
                raise ValueError("ExifTool returned an invalid Metadata JSON document.")
            return decoded
        try:
            if result is None:
                if should_cancel and should_cancel():
                    return {path:InterruptedError("Metadata read cancelled.") for path in paths}
                cmd=[self.exiftool,*arguments]
                result=subprocess.run(
                    cmd,check=False,capture_output=True,
                    timeout=self.read_timeout,**_NO_CONSOLE,
                )
            # ExifTool JSON is UTF-8. Capturing bytes and decoding here keeps a
            # Windows legacy code page from crashing subprocess's background
            # reader thread and silently discarding an otherwise valid batch.
            payload=decode(result)
        except Exception as exc:
            if used_persistent:
                persistent_error=exc
                try:self.close(force=True)
                except Exception as shutdown_error:
                    return {path:RuntimeError(f"Metadata reader shutdown incomplete: {shutdown_error}") for path in paths}
                try:
                    if should_cancel and should_cancel():
                        return {path:InterruptedError("Metadata read cancelled.") for path in paths}
                    cmd=[self.exiftool,*arguments]
                    result=subprocess.run(
                        cmd,check=False,capture_output=True,
                        timeout=self.read_timeout,**_NO_CONSOLE,
                    )
                    payload=decode(result)
                except Exception as recovery_exc:
                    if isinstance(recovery_exc,subprocess.TimeoutExpired):
                        recovery_exc=TimeoutError(
                            "ExifTool did not finish the one-shot Metadata batch "
                            f"within {self.read_timeout:g} seconds."
                        )
                    error=RuntimeError(
                        f"Persistent Metadata read failed ({persistent_error}); "
                        f"one-shot recovery also failed ({recovery_exc})."
                    )
                    return {path:error for path in paths}
            else:
                if isinstance(exc,subprocess.TimeoutExpired):
                    exc=TimeoutError(
                        "ExifTool did not finish the one-shot Metadata batch "
                        f"within {self.read_timeout:g} seconds."
                    )
                error=exc if persistent_error is None else RuntimeError(
                    f"Persistent Metadata read failed ({persistent_error}); "
                    f"one-shot recovery also failed ({exc})."
                )
                return {path:error for path in paths}
        stderr_value=getattr(result,"stderr","") or ""
        stderr=(
            stderr_value.decode("utf-8","replace")
            if isinstance(stderr_value,bytes) else str(stderr_value)
        )
        items=[item for item in payload if isinstance(item,dict)] if isinstance(payload,list) else []
        result_by_path: dict[Path,dict[str,object]|Exception]={}
        keyed: dict[str,list[dict]]={}; unkeyed=[]
        for item in items:
            source_file=str(item.get("SourceFile") or "").strip()
            if source_file:
                key=os.path.normcase(os.path.abspath(source_file))
                keyed.setdefault(key,[]).append(item)
            else:
                # Older/mocked ExifTool responses may omit SourceFile. Only
                # those records may use request-order fallback; a record that
                # names another file must never cross image authority.
                unkeyed.append(item)
        for path in paths:
            target=os.path.normcase(os.path.abspath(path))
            matches=keyed.get(target,[])
            item=matches.pop(0) if matches else unkeyed.pop(0) if unkeyed else None
            if item is None:
                detail=self._read_error_detail(stderr,int(getattr(result,"returncode",0) or 0))
                result_by_path[path]=RuntimeError(detail)
                continue
            if item.get("Error"):
                result_by_path[path]=RuntimeError(str(item["Error"]))
                continue
            try:
                result_by_path[path]=self._parse_embedded_item(item)
            except Exception as exc:
                result_by_path[path]=exc
        return result_by_path

    @staticmethod
    def _read_error_detail(stderr: str,returncode: int) -> str:
        lines=[]
        for raw in str(stderr or "").splitlines():
            value=raw.strip()
            # ExifTool writes successful batch counts to stderr even with JSON
            # output. They are status summaries, not per-image failures.
            words=value.casefold().split()
            if len(words)==4 and words[0].isdigit() and words[1:]==["image","files","read"]:
                continue
            if value:
                lines.append(value)
        if lines:
            return "\n".join(lines)
        if int(returncode or 0):
            return f"ExifTool returned no Metadata record (exit code {returncode})."
        return "ExifTool returned no Metadata record for this image."

    @staticmethod
    def _parse_embedded_item(item: dict) -> dict[str,object]:

        tags: list[str] = []
        prose: list[str] = []
        ambiguous_fragments: list[str] = []
        type_issues: list[str] = []
        for key in ("Subject", "HierarchicalSubject", "Keywords", "XPKeywords"):
            value = item.get(key)
            values = value if isinstance(value, list) else [value] if isinstance(value, str) else []
            for part in values:
                raw_part=str(part).strip()
                # Embedded commas inside a quoted phrase can make an otherwise
                # clear sentence look like two independent keywords. ExifTool
                # returns several legacy keyword fields as one string, and CSV
                # only treats a quote as structural when it begins the field.
                # Preserve the complete observation for review instead of
                # promoting either comma fragment as an SDXL Tag.
                broken_quotes=has_broken_metadata_quotes(raw_part)
                quoted_sentence=(
                    "," in raw_part and any(mark in raw_part for mark in ('"','“','”'))
                    and looks_like_metadata_prose_fragment(raw_part)
                )
                if broken_quotes or quoted_sentence:
                    ambiguous_fragments.append(raw_part)
                    type_issues.append(
                        f"{key} is a tag field but contains "
                        +("broken quotation or punctuation data" if broken_quotes else "quoted sentence-like content")
                        +"; it was not promoted."
                    )
                    continue
                try:
                    fragments=next(csv.reader([raw_part]))
                except Exception:
                    fragments=[raw_part]
                if key=="XPKeywords":
                    fragments=[piece for fragment in fragments for piece in re.split(r"\s*;\s*",fragment)]
                for fragment in fragments:
                    clean=str(fragment).strip()
                    if not clean:
                        continue
                    if looks_like_metadata_prose_fragment(clean):
                        ambiguous_fragments.append(clean)
                        type_issues.append(
                            f"{key} is a tag field but contains sentence-like content; it was not promoted."
                        )
                    else:
                        tags.append(clean)

        for key in ("Description", "Caption-Abstract", "ImageDescription"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                clean=value.strip()
                if looks_like_clear_tag(clean):
                    ambiguous_fragments.append(clean)
                    type_issues.append(
                        f"{key} is a description field but contains strong tag syntax; it was not promoted."
                    )
                else:
                    prose.append(clean)
        generation_info: list[str] = []
        generation_records: list[dict] = []
        raw_generation_fields: dict[str, object] = {}
        for key in ("Parameters", "Comment", "Prompt", "Workflow"):
            value = item.get(key)
            if not isinstance(value, (str, dict, list)) or not str(value).strip():
                continue
            # Never normalize the source payload. The parser owns its derived
            # view while this exact value remains the regeneration reference.
            raw_value = value
            raw_generation_fields[key] = raw_value
            generation_info.append(raw_value if isinstance(raw_value, str) else json.dumps(raw_value, ensure_ascii=False))
            generation_records.append(
                parse_generation_payload(
                    raw_value, source="Image metadata", source_field=key
                ).to_dict()
            )
        for generation_record in generation_records:
            if not generation_record.get("width") and item.get("ImageWidth"):
                generation_record["width"] = item.get("ImageWidth")
            if not generation_record.get("height") and item.get("ImageHeight"):
                generation_record["height"] = item.get("ImageHeight")
            stages = generation_record.get("stages", [])
            if isinstance(stages, list):
                first_stage = next(
                    (
                        stage for stage in stages
                        if isinstance(stage, dict) and stage.get("kind") == "first_pass"
                    ),
                    None,
                )
                if first_stage is not None:
                    if not first_stage.get("width") and generation_record.get("width"):
                        first_stage["width"] = generation_record["width"]
                    if not first_stage.get("height") and generation_record.get("height"):
                        first_stage["height"] = generation_record["height"]
            software = item.get("Software") or item.get("CreatorTool")
            if software:
                generation_record.setdefault("extras", {})["Generator software"] = software
            # Generation prompts/settings are provenance. They may contain
            # model syntax or workflow instructions and are not confirmed
            # descriptions of the visible image.
        # Some image tools store a shortened copy of a long XMP keyword in IPTC.
        # Keep the complete version rather than showing both as separate prose.
        cleaned_prose: list[str] = []
        for value in TagCollection(prose).to_list():
            key = value.casefold().strip()
            if len(key) >= 20 and any(
                key != other.casefold().strip()
                and other.casefold().strip().startswith(key)
                for other in prose
            ):
                continue
            cleaned_prose.append(value)
        return {
            "tags": TagCollection(tags).to_list(),
            "plain_language": cleaned_prose,
            "ambiguous_fragments": TagCollection(ambiguous_fragments).to_list(),
            "type_issues": list(dict.fromkeys(type_issues)),
            "tag_fields": {
                key: item[key]
                for key in ("Subject", "HierarchicalSubject", "Keywords", "XPKeywords")
                if key in item
            },
            "description_fields": {
                key: item[key]
                for key in ("Description", "Caption-Abstract", "ImageDescription")
                if key in item
            },
            "generation_info": generation_info,
            "generation_records": generation_records,
            "raw_generation_fields": raw_generation_fields,
            "standard_metadata": {
                key: item[key]
                for key in (
                    "ImageWidth", "ImageHeight", "FileType", "MIMEType",
                    "Orientation", "ColorSpace", "ColorType", "BitsPerSample",
                    "ProfileDescription", "Software", "CreatorTool",
                )
                if key in item
            },
        }
