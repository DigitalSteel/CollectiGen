from __future__ import annotations

from pathlib import Path
from threading import RLock

from PySide6.QtCore import QObject,Signal

from core import (
    ImageRecord,RATING_ORDER,prohibited_content_signal,
    protective_display_rating,typed_tag_sources,
)


class GalleryContentSafetyPolicy(QObject):
    """Apply gallery ratings, blurring, and session-only reveals."""

    saved_result_published=Signal(object)

    def __init__(self,settings,knowledge,dataset=None,categories=None):
        super().__init__()
        self.settings=settings; self.knowledge=knowledge; self.dataset=dataset; self.categories=categories
        self._cache={}; self._latest={}; self._basis={}; self._safety={}; self._pending_validation=set(); self._revealed=set(); self._lock=RLock()
        self._generation=0; self._revisions={}

    @staticmethod
    def _path_key(path) -> str:
        return str(Path(path))

    def clear_cache(self) -> None:
        with self._lock:
            self._generation+=1
            self._cache.clear(); self._latest.clear(); self._basis.clear(); self._safety.clear(); self._pending_validation.clear()

    def invalidate(self,paths=None) -> None:
        """Forget changed paths without forcing unchanged ratings to be rescanned."""
        if paths is None:
            self.clear_cache(); return
        keys={self._path_key(value) for value in paths}
        with self._lock:
            for key in keys:
                self._revisions[key]=self._revisions.get(key,0)+1
                self._latest.pop(key,None); self._basis.pop(key,None); self._safety.pop(key,None); self._pending_validation.discard(key)
            self._cache={key:value for key,value in self._cache.items() if key[0] not in keys}

    def _revision_token(self,path) -> tuple[int,int]:
        key=self._path_key(path)
        with self._lock:
            return self._generation,self._revisions.get(key,0)

    def display_rating_for_record(self,record: ImageRecord) -> tuple[str,str]:
        """Resolve one already-loaded record without changing shared state."""
        return protective_display_rating(
            typed_tag_sources(record,self.knowledge),self.knowledge,self.categories,
        )

    def _remember_safety(self,path,tag_sources) -> tuple[str,str]:
        status,tags=prohibited_content_signal(tag_sources,self.knowledge)
        detail=""
        if status=="confirmed":
            detail=(
                "Confirmed Image Knowledge matches a reviewed prohibited-content "
                "rule. Open the image-data editor to review or correct it."
            )
        elif status=="suspected":
            detail=(
                "An unconfirmed AI or imported tag matches a reviewed prohibited-content "
                "rule. This is not a confirmed fact; review it before sharing."
            )
        with self._lock:
            self._safety[self._path_key(path)]=(status,detail,tags)
        return status,detail

    def cached_safety(self,path) -> tuple[str,str]:
        with self._lock:
            status,detail,_tags=self._safety.get(
                self._path_key(path),("","",()),
            )
        return status,detail

    def publish_saved_record(self,record: ImageRecord) -> tuple[str,str]:
        """Publish one successful editor save and supersede older work."""
        image=Path(record.image_path)
        try:
            rating,basis=self.display_rating_for_record(record)
            sources=typed_tag_sources(record,self.knowledge)
            self._remember_safety(image,sources)
        except Exception:
            rating,basis=(
                "unknown","Saved image content rating could not be resolved",
            )
        path_key=self._path_key(image)
        with self._lock:
            self._revisions[path_key]=self._revisions.get(path_key,0)+1
            self._cache={key:value for key,value in self._cache.items() if key[0]!=path_key}
            self._latest[path_key]=rating; self._basis[path_key]=basis
            self._pending_validation.discard(path_key)
        self.saved_result_published.emit(image)
        return rating,basis

    def prime_saved_records(self,records) -> int:
        """Expose remembered ratings immediately while freshness validates.

        Saved values remain presentation hints, not authority.  With the
        default fail-safe policy, every pending value stays blurred until its
        exact image/TXT/sidecar signature is cheaply confirmed.
        """
        count=0
        with self._lock:
            for record in records:
                if getattr(record,"error",""):
                    rating,basis="unknown","Saved rating was incomplete or unreadable"
                else:
                    rating,basis=protective_display_rating(record.tag_sources,self.knowledge,self.categories)
                    self._remember_safety(record.path,record.tag_sources)
                key=self._path_key(record.path)
                self._latest[key]=rating; self._basis[key]="Saved result; checking freshness • "+basis
                self._pending_validation.add(key); count+=1
        return count

    def confirm_validated_records(self,records) -> int:
        """Accept rows whose exact file signatures were validated elsewhere.

        The remembered-library refresh already validates image, TXT, and JSON
        signatures before publishing ``validated_records``. Recomputing the
        rating from that same typed row is sufficient; reopening the sidecar or
        running a second per-image filesystem check would duplicate startup
        work without adding freshness evidence.
        """
        count=0
        for record in records:
            image=Path(record.path)
            if getattr(record,"error",""):
                rating,basis="unknown","Validated image data was incomplete or unreadable"
            else:
                rating,basis=protective_display_rating(
                    record.tag_sources,self.knowledge,self.categories,
                )
                self._remember_safety(record.path,record.tag_sources)
            key=(str(image),"validated",id(record),len(self.knowledge.tags))
            self._remember(image,rating,basis,key); count+=1
        return count

    def cached_rating(self,path) -> str | None:
        with self._lock:
            return self._latest.get(self._path_key(path))

    def cached_basis(self,path) -> str:
        with self._lock:
            return self._basis.get(self._path_key(path),"")

    def mark_unavailable(self,paths,basis: str="Content-rating evidence could not be checked") -> None:
        """End a failed or cancelled checking state as a protected Unknown result."""
        keys={self._path_key(value) for value in paths}
        with self._lock:
            for key in keys:
                self._latest[key]="unknown"; self._basis[key]=basis; self._pending_validation.discard(key)
            self._cache={key:value for key,value in self._cache.items() if key[0] not in keys}

    def unclassified(self,paths) -> list[Path]:
        with self._lock:
            known=set(self._latest); pending=set(self._pending_validation)
        return [Path(value) for value in paths if self._path_key(value) not in known or self._path_key(value) in pending]

    @staticmethod
    def _signatures(image: Path) -> tuple[tuple[int,int],tuple[int,int],tuple[int,int]]:
        def signature(path: Path) -> tuple[int,int]:
            try:
                stat=path.stat(); return stat.st_size,stat.st_mtime_ns
            except OSError:
                return -1,-1
        return signature(image),signature(image.with_suffix(".txt")),signature(image.with_suffix(".tags.json"))

    def _remember(
        self,image: Path,rating: str,basis: str,key,*,expected_revision=None,
    ) -> str:
        with self._lock:
            path_key=self._path_key(image)
            if (
                expected_revision is not None
                and expected_revision!=(self._generation,self._revisions.get(path_key,0))
            ):
                return self._latest.get(path_key,"unknown")
            self._cache[key]=rating; self._latest[self._path_key(image)]=rating
            self._basis[self._path_key(image)]=basis
            self._pending_validation.discard(self._path_key(image))
        return rating

    def rating_for(self,path: str|Path,*,db=None) -> str:
        image=Path(path)
        expected_revision=self._revision_token(image)
        dataset=self.dataset
        cached=(getattr(dataset,"validated_records",{}).get(image) if dataset is not None else None)
        key=(str(image),"validated",id(cached),len(self.knowledge.tags)) if cached is not None else (
            str(image),self._signatures(image),len(self.knowledge.tags)
        )
        with self._lock:
            if (
                key in self._cache
                and expected_revision==(
                    self._generation,self._revisions.get(self._path_key(image),0)
                )
            ):
                rating=self._cache[key]; self._latest[self._path_key(image)]=rating; return rating
        if dataset is not None:
            index=getattr(dataset,"index",None)
            if cached is None and index is not None:
                try: cached=index.get_if_fresh(image,db)
                except Exception: cached=None
        if cached is not None and not cached.error:
            rating,basis=protective_display_rating(cached.tag_sources,self.knowledge,self.categories)
            self._remember_safety(image,cached.tag_sources)
            return self._remember(
                image,rating,basis,key,expected_revision=expected_revision,
            )
        rating="unknown"; basis="No dependable rating yet"
        try:
            record=ImageRecord(image)
            sources=typed_tag_sources(record,self.knowledge)
            rating,basis=protective_display_rating(
                sources,self.knowledge,self.categories,
            )
            self._remember_safety(image,sources)
        except Exception:
            basis="Rating evidence is unavailable or unreadable"
        return self._remember(
            image,rating,basis,key,expected_revision=expected_revision,
        )

    def classify_many(self,paths,progress=None,should_cancel=None) -> dict[str,object]:
        """Classify with one cache connection and reuse persisted fresh rows."""
        items=[Path(value) for value in paths]; checked=0; reused=0
        index=getattr(self.dataset,"index",None) if self.dataset is not None else None
        validated=getattr(self.dataset,"validated_records",{}) if self.dataset is not None else {}
        connection=index.connect() if index is not None and any(item not in validated for item in items) else None
        try:
            for position,image in enumerate(items,1):
                if should_cancel and should_cancel():
                    return {"paths":items,"checked":checked,"reused":reused,"cancelled":True}
                before=self.cached_rating(image)
                self.rating_for(image,db=connection); checked+=1
                if before is not None or image in validated:
                    reused+=1
                if progress and (position==len(items) or position%25==0):
                    progress(position,len(items),image.name)
        finally:
            if connection is not None: connection.close()
        return {"paths":items,"checked":checked,"reused":reused,"cancelled":False}

    @staticmethod
    def _key(path) -> str:
        try: return str(Path(path).resolve())
        except OSError: return str(Path(path))

    def visible(self,path) -> bool:
        # Rating preferences never remove an image from gallery paging.  The
        # persisted field retains its old name for settings compatibility, but
        # its user-facing meaning is now an exact set of ratings to blur.
        return True

    def is_revealed(self,path) -> bool:
        return getattr(self.settings,"remember_content_reveals",True) and self._key(path) in self._revealed

    def should_blur(self,path) -> bool:
        key=self._path_key(path)
        with self._lock:
            pending=key in self._pending_validation; rating=self._latest.get(key)
        if pending and self.protects_unknown_ratings():
            return not self.is_revealed(path)
        rating=rating if rating is not None else self.rating_for(path)
        return not self.is_revealed(path) and self._rating_requires_blur(rating)

    def preview_state(self,path,*,honor_session_reveal: bool=True) -> dict[str,object]:
        """Return the current in-memory display state without reading user data.

        Galleries may honor their optional application-session reveal memory.
        Full-size and secondary previews pass ``honor_session_reveal=False`` so
        each preview starts protected and owns only a temporary local reveal.
        """
        key=self._path_key(path)
        with self._lock:
            pending=key in self._pending_validation
            rating=self._latest.get(key)
            basis=self._basis.get(key,"")
        had_rating=rating is not None
        checking=pending or not had_rating
        if rating is None:
            rating="unknown"
            basis=basis or "Content rating is still checking"
        requires_blur=(
            (
                self.protects_unknown_ratings()
                or (had_rating and self._rating_requires_blur(rating))
            )
            if pending else
            self._rating_requires_blur(rating)
        )
        session_revealed=bool(
            honor_session_reveal and self.is_revealed(path)
        )
        return {
            "rating":rating,
            "basis":basis,
            "checking":checking,
            "requires_blur":bool(requires_blur),
            "blurred":bool(requires_blur and not session_revealed),
        }

    def preview_state_for_record(
        self,record: ImageRecord,*,honor_session_reveal: bool=True,
    ) -> dict[str,object]:
        """Combine shared state with one already-loaded current record.

        The universal editor already has the Image Knowledge record it shows.
        Use that exact record to avoid presenting a settled cache miss as if the
        visible confirmed rating did not exist. Pending freshness validation
        remains fail-safe and cannot be bypassed by an in-memory record.
        """
        image=Path(record.image_path)
        shared=self.preview_state(
            image,honor_session_reveal=honor_session_reveal,
        )
        with self._lock:
            pending=self._path_key(image) in self._pending_validation
        if pending or bool(shared.get("checking")):
            return shared
        try:
            record_rating,record_basis=self.display_rating_for_record(record)
        except Exception:
            return shared
        shared_rating=str(shared.get("rating") or "unknown")
        rating=shared_rating
        basis=str(shared.get("basis") or "")
        if (
            shared_rating=="unknown"
            and basis!="No dependable rating yet"
        ):
            return shared
        if record_rating in RATING_ORDER and (
            shared_rating not in RATING_ORDER
            or RATING_ORDER[record_rating]>RATING_ORDER[shared_rating]
        ):
            rating=record_rating; basis=record_basis
        elif shared_rating not in RATING_ORDER:
            rating=record_rating; basis=record_basis
        requires_blur=self._rating_requires_blur(rating)
        session_revealed=bool(
            honor_session_reveal and self.is_revealed(image)
        )
        return {
            "rating":rating,
            "basis":basis,
            "checking":False,
            "requires_blur":bool(requires_blur),
            "blurred":bool(requires_blur and not session_revealed),
        }

    def _rating_requires_blur(self,rating: str) -> bool:
        if rating=="unknown":
            return bool(getattr(self.settings,"gallery_blur_unknown",True))
        forced=set(getattr(self.settings,"gallery_hidden_ratings",()))
        allowed=str(getattr(self.settings,"gallery_content_level","explicit"))
        return rating in forced or RATING_ORDER.get(rating,0)>RATING_ORDER.get(allowed,3)

    def protects_unknown_ratings(self) -> bool:
        return bool(getattr(self.settings,"gallery_blur_unknown",True))

    def remember_reveal(self,path) -> None:
        if getattr(self.settings,"remember_content_reveals",True): self._revealed.add(self._key(path))

    def forget_reveal(self,path) -> None:
        self._revealed.discard(self._key(path))

    def clear_session_reveals(self) -> None:
        self._revealed.clear()

    def __call__(self,path,card):
        def apply_blur(blurred: bool,rating: str,required: bool) -> None:
            setter=getattr(card,"set_content_protection_required",None)
            if callable(setter):
                setter(required)
            card.set_content_blurred(blurred,rating)

        rating=self.cached_rating(path)
        safety_status,safety_detail=self.cached_safety(path)
        safety_setter=getattr(card,"set_safety_review",None)
        if callable(safety_setter):
            safety_setter(safety_status,safety_detail)
        if rating is not None and hasattr(card,"set_content_rating"): card.set_content_rating(rating,self.cached_basis(path))
        if getattr(card,"_content_safety_signal_owner",None) is not self:
            if hasattr(card,"revealed"): card.revealed.connect(self.remember_reveal)
            if hasattr(card,"hidden_again"): card.hidden_again.connect(self.forget_reveal)
            card._content_safety_signal_owner=self
        with self._lock:
            pending=self._path_key(path) in self._pending_validation
        if rating is None:
            required=self.protects_unknown_ratings()
            apply_blur(required,"checking",required)
        elif pending and self.protects_unknown_ratings():
            apply_blur(
                not self.is_revealed(path),"checking saved rating",True
            )
        else:
            required=self._rating_requires_blur(rating)
            apply_blur(
                not self.is_revealed(path) and required,rating,required
            )
