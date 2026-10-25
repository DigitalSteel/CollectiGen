"""Canonical generation entry points and ordinary Ingest handoff."""
from __future__ import annotations

from pathlib import Path
from copy import deepcopy
import re

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog
from .status_notifications import FooterMessageBox as QMessageBox
from core.annotation import GenerationResource
from core.generation_execution import digest
from core.image_record import ImageRecord
from core.model_resource_registry import ModelResourceRegistry
from core.paths import MODEL_RESOURCES_PATH, SWARM_RUNTIME_PATH, USER_DATA_DIR
from core.training_source_io import read_stable_training_source
from core.video_generation import DerivedFrameReference


def _host(widget):
    current = widget
    host = None
    while current is not None:
        if getattr(current, "state", None) is not None and hasattr(current.state, "jobs"):
            host = current
        current = current.parent()
    if host is None:
        raise ValueError("Open generation from the application workspace.")
    return host


def _setup_window(host):
    current = host
    while current is not None:
        if callable(getattr(current, "open_swarm_setup", None)):
            return current
        current = current.parent()
    return None


def _saved_runtime_connection(setup_window):
    """Read enrolled loopback metadata only; this never checks or starts SwarmUI."""
    from core.generation_transport import local_endpoint
    from core.swarm_runtime import SwarmRuntimeManager

    panel = getattr(getattr(setup_window, "setup_page", None), "swarm_runtime", None)
    manager = getattr(panel, "manager", None) or SwarmRuntimeManager(
        record_path=USER_DATA_DIR / SWARM_RUNTIME_PATH.name)
    try:
        snapshot = manager.snapshot()
        if not snapshot.configured:
            return "", ""
        if snapshot.ownership not in {"adopted", "managed"}:
            return "", "Saved generator connection needs review in Setup before checking it."
        return local_endpoint(snapshot.endpoint), ""
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return "", "Saved generator connection is unavailable. Review it in Setup before checking it."


def _connect_setup_return(window, dialog):
    """Route one retained dialog through the existing Setup workspace."""
    def open_setup():
        if (getattr(window, "_generation_setup_return", None) is not None
                or getattr(window, "_model_import_setup_return", None) is not None
                or getattr(window, "_image_setup_return", None) is not None):
            dialog.status.setText("Another workflow is waiting in Setup. Return to it before continuing.")
            return
        origin = window.navigation.currentRow()
        try:
            window.open_swarm_setup()
            panel = window.setup_page.swarm_runtime
        except Exception as exc:
            window.navigation.setCurrentRow(origin)
            dialog.status.setText(f"Plan saved, but Setup could not open: {exc}")
            return

        def detach(*_args):
            if getattr(window, "_generation_setup_return", None) is not resume:
                return
            window._generation_setup_return = None
            panel.set_request_return_available(False)
            panel.return_requested.disconnect(resume)
            dialog.finished.disconnect(detach)
            dialog.workspace_closed.disconnect(detach)

        def resume():
            try:
                endpoint, issue = _saved_runtime_connection(window)
                dialog.resume_from_setup(endpoint, issue)
            except Exception as exc:
                # A damaged/unavailable management record cannot lose the saved plan.
                dialog.resume_from_setup("", "Saved generator connection is unavailable. Review it in Setup before checking it.")
                dialog.status.setText(f"Plan retained; review the connection because Setup status is unavailable: {exc}")
            detach()
            window.navigation.setCurrentRow(origin)
            dialog.raise_(); dialog.activateWindow()
            dialog.check_button.setFocus()

        window._generation_setup_return = resume
        panel.return_requested.connect(resume)
        dialog.finished.connect(detach)
        dialog.workspace_closed.connect(detach)
        panel.set_request_return_available(True)
        dialog.hide()

    dialog.setup_requested.connect(open_setup)

    def open_image_description_setup(review):
        if (getattr(window, "_generation_setup_return", None) is not None
                or getattr(window, "_model_import_setup_return", None) is not None
                or getattr(window, "_image_setup_return", None) is not None):
            review.suggestion_status.setText(
                "Another workflow is waiting in Setup. Return to it before continuing."
            )
            return
        origin=window.navigation.currentRow()
        review.hide(); dialog.hide()
        try:
            window.navigation.setCurrentRow(6)
            window._ensure_page(6)
            panel=window.setup_page
            panel.show_section("ai","description_model")
            panel.set_image_setup_return_available(True)
        except Exception as exc:
            window.navigation.setCurrentRow(origin)
            dialog.show(); review.show()
            review.suggestion_status.setText(
                f"Setup could not open: {exc} Your Generation Setup choices remain here."
            )
            return

        def detach(*_args):
            if getattr(window,"_image_setup_return",None) is not resume:
                return
            window._image_setup_return=None
            panel.set_image_setup_return_available(False)
            panel.image_setup_return_requested.disconnect(resume)
            review.finished.disconnect(detach)
            dialog.workspace_closed.disconnect(detach)

        def resume():
            if not panel.confirm_leave():
                return False
            detach()
            window.navigation.setCurrentRow(origin)
            dialog.show(); review.show()
            review.raise_(); review.activateWindow()
            review.refresh_suggestion_goals()
            review.review_description_setup_button.hide()
            try:
                source_current=(
                    review.source_current_provider()
                    if callable(review.source_current_provider)
                    else review.source_identity
                )
                if source_current!=review.source_identity:
                    review.suggest_button.setEnabled(False)
                    review.save_button.setEnabled(False)
                    review.suggestion_status.setText(
                        "The selected image changed while Setup was open. Close this review, "
                        "reselect the image, and start a fresh setup. Your unsaved choices were not applied."
                    )
                else:
                    review.suggestion_status.setText(
                        "Returned from Setup with your choices retained. Choose Suggest Text from Image "
                        "for a fresh local check, or continue with manual and saved text."
                    )
            except Exception as exc:
                review.suggest_button.setEnabled(False)
                review.save_button.setEnabled(False)
                review.suggestion_status.setText(
                    f"The selected image could not be rechecked: {exc} Reselect it before saving."
                )
            review.suggest_button.setFocus()
            return True

        window._image_setup_return=resume
        panel.image_setup_return_requested.connect(resume)
        review.finished.connect(detach)
        dialog.workspace_closed.connect(detach)
        panel.image_setup_return_panel.action_button.setFocus()

    dialog.image_setup_requested.connect(open_image_description_setup)


def _source_identity(paths, *, frame_reference_provider=None):
    identities = []
    for path in paths:
        _, image = read_stable_training_source(path)
        sidecar = path.with_suffix(".tags.json")
        knowledge_hash = None
        if sidecar.is_file():
            _, knowledge = read_stable_training_source(sidecar)
            knowledge_hash = knowledge["sha256"]
        identities.append({"image_sha256": image["sha256"], "knowledge_sha256": knowledge_hash})
    identity = {"collection": identities, "fingerprint": digest(identities), "protected": False}
    if frame_reference_provider is not None:
        if not callable(frame_reference_provider) or len(identities) != 1:
            raise ValueError("A derived frame needs one exact source and provenance provider.")
        value = frame_reference_provider()
        reference = value if isinstance(value, DerivedFrameReference) else DerivedFrameReference.from_dict(value)
        if reference.output_sha256 != identities[0]["image_sha256"]:
            raise ValueError("The extracted video frame differs from the selected image.")
        identity["derived_frame"] = reference.to_dict()
        identity["fingerprint"] = digest({"collection": identities,
                                          "derived_frame": identity["derived_frame"]})
    return identity


def _assert_portable_provenance(value, *, location="registered resource", depth=0, budget=None):
    """Fail closed before machine-local registry detail enters a portable plan."""
    if budget is None:
        budget = [20_000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 64:
        raise ValueError("The registered model provenance is too complex for a portable generation plan.")
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_portable_provenance(key, location=f"{location} field name", depth=depth + 1, budget=budget)
            _assert_portable_provenance(item, location=f"{location}.{key}", depth=depth + 1, budget=budget)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_portable_provenance(item, location=f"{location}[{index}]", depth=depth + 1, budget=budget)
    elif isinstance(value, str):
        without_urls = re.sub(r"https?://\S+", "", value, flags=re.IGNORECASE)
        if (re.search(r"(?<![a-zA-Z0-9])[a-zA-Z]:[\\/]", value)
                or re.search(r"\\\\[^\\\s]+[\\/]", value)
                or re.search(r"(?<![a-zA-Z0-9])/(?:[^/\s]+)(?:/[^\s]*)?", without_urls)):
            raise ValueError(
                f"The registered model provenance contains a machine-local path at {location}. "
                "Review its lineage in Models before creating a portable generation plan."
            )


def _registered_resource(registry, resource_id, *, selected_role="", selected_family=""):
    """Return one portable typed reference without retaining registry paths."""
    resource, _label = _registered_resource_with_label(registry, resource_id,
        selected_role=selected_role, selected_family=selected_family)
    return resource


def _registered_resource_with_label(registry, resource_id, *, selected_role="", selected_family=""):
    """Read one exact resource and a path-free presentation label together."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(resource_id)):
        raise ValueError("This generation adapter requires a single-file SHA-256 resource; preparation packs stay in Setup.")
    value, _revision = registry.snapshot()
    record = value["resources"].get(resource_id)
    if record is None:
        raise ValueError("The selected registered model is no longer in the model registry.")
    declarations = deepcopy(record.get("declarations", []))
    if selected_role:
        declarations = [item for item in declarations if item.get("role") == selected_role
            and (not selected_family or item.get("family") == selected_family)]
    roles = {str(item.get("role", "")).strip() for item in declarations if item.get("role")}
    if len(roles) != 1:
        raise ValueError("The selected registered model does not have one unambiguous resource role.")
    role = roles.pop()
    families = sorted({str(item.get("family", "")).strip() for item in declarations
                       if item.get("role") == role and str(item.get("family", "")).strip()})
    lineage = deepcopy(record.get("lineage", []))
    _assert_portable_provenance({"declarations": declarations, "lineage": lineage})
    sha256 = resource_id.removeprefix("sha256:")
    resource = GenerationResource(
        id=resource_id,
        role=role,
        name=f"Registered {role.replace('_', ' ').title()}",
        hashes={"sha256": sha256},
        source_id=resource_id,
        compatibility=families,
        provenance={
            "authority": "model_resource_registry",
            "resource_id": resource_id,
            "declarations": declarations,
            "lineage": lineage,
        },
    )
    user = record.get("user", {})
    custom = str(user.get("display_name", "") or "").strip() if isinstance(user, dict) else ""
    locations = record.get("locations", ())
    basename = next((Path(item["path"]).name for item in locations
                     if isinstance(item, dict) and item.get("path")), "")
    label = custom if _safe_model_label(custom) else basename if _safe_model_label(basename) else resource_id
    return resource, label


def _safe_model_label(value):
    return isinstance(value, str) and bool(value) and len(value) <= 160 and not any(
        char in value for char in ("/", "\\", "\r", "\n", "\t")) and all(ord(char) >= 32 for char in value)


def _resource_label_key(resource):
    compatibility = resource.get("compatibility", ())
    family = compatibility[0] if isinstance(compatibility, (list, tuple)) and len(compatibility) == 1 else ""
    return resource.get("id", ""), resource.get("role", ""), family


def _updated_resource_labels(context, resources, *, selected=None, label=""):
    """Retain only labels tied to current exact declarations; never alter request resources."""
    target = dict(context)
    current = {_resource_label_key(item) for item in resources}
    previous = target.get("resource_display_labels", ())
    entries = [item for item in previous if isinstance(item, dict) and
               (item.get("resource_id"), item.get("role"), item.get("family")) in current and
               _safe_model_label(item.get("label"))] if isinstance(previous, (list, tuple)) else []
    if selected is not None and _safe_model_label(label):
        key = _resource_label_key(selected)
        entries = [item for item in entries if
                   (item["resource_id"], item["role"], item["family"]) != key]
        entries.append(dict(resource_id=key[0], role=key[1], family=key[2], label=label))
    if entries:
        target["resource_display_labels"] = entries
    else:
        target.pop("resource_display_labels", None)
    return target


def _resource_display_label(resource, context):
    key = _resource_label_key(resource.to_dict())
    values = context.get("resource_display_labels", ())
    if isinstance(values, (list, tuple)):
        for item in values:
            if isinstance(item, dict) and (item.get("resource_id"), item.get("role"), item.get("family")) == key:
                label = item.get("label")
                if isinstance(label, str) and _safe_model_label(label):
                    return label
    return resource.name


def _apply_finishing_draft(target, value):
    """A chosen winner starts a native-canvas Remix with a separate final-size goal."""
    finish = value.get("finish_context")
    if not isinstance(finish, dict) or not finish.get("output_goal"):
        return
    context = target.plan.target_context
    context.pop("output_plan", None)
    context["output_goal"] = deepcopy(finish["output_goal"])
    changes = {"target_context": context}
    source_plan = value.get("finish_source_plan")
    if source_plan is not None:
        request = source_plan.request.to_dict()
        for field in ("model", "model_hash", "vae", "steps", "sampler", "scheduler",
                      "cfg_scale", "seed", "positive_prompt", "negative_prompt", "resources"):
            changes[field] = deepcopy(request[field])
    target.plan = target.plan.revise(changes, source="selected_candidate_finishing")
    target.remix_strength.setValue(float(value.get("finish_strength", .25)))
    target._image_task_changed()
    target.prepared = None
    target._refresh_from_plan("Winner opened for refinement at its source size. Review the final size, then preview and apply its output stages.")


def choose_generation_draft(parent):
    """Choose and validate a saved request before adding a Generate workspace."""
    from core.generation_planning import load_plan_with_digest
    from .generation_saved_work_dialog import SavedGenerationWorkDialog

    host = _host(parent)
    browser = SavedGenerationWorkDialog(
        host.state.jobs, USER_DATA_DIR / "generation", "drafts",
        parent=parent, selection_only=True,
    )
    if browser.exec() != QDialog.Accepted:
        return
    if browser.requested_action == "create":
        open_generation_workspace(parent, force_new=True)
        return
    if not browser.selected_path:
        return
    target = Path(browser.selected_path).absolute()
    try:
        plan, token = load_plan_with_digest(target)
        if not browser.selected_digest or token != browser.selected_digest:
            raise ValueError("This saved draft changed since the list was shown. Refresh and choose it again.")
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
        QMessageBox.information(
            parent, "Image draft needs attention",
            "The selected draft could not be resumed. Existing work was not changed. "
            f"Choose Resume Image Draft again after checking the file.\n\n{exc}",
        )
        return
    open_generation_workspace(parent, force_new=True,
                              preloaded_draft=(plan, target, token))


def open_generation_workspace(parent, *, paths=(), record=None, resource_id="", selected_role="", selected_family="", remix=False, force_new=False, on_opened=None, initial_plan=None, source_context=None, initial_task="remix", initial_image_intent=None, repair_context=None, setup_from_image=False, preloaded_draft=None):
    """Bounded selected-image context; source hashing and loading use shared I/O."""
    if repair_context is not None:
        allowed = {"reason", "original_location", "source_workspace"}
        if (not isinstance(repair_context, dict) or set(repair_context) - allowed
                or any(not isinstance(value, str) for value in repair_context.values())):
            QMessageBox.information(parent, "Invalid repair context",
                "Repair context must contain only the recorded reason and source location as text.")
            return
        repair_context = {key: repair_context.get(key, "")[:4096] for key in allowed}
    host = _host(parent)
    setup_window = _setup_window(host)
    requested_scope = bool(paths or record is not None or resource_id or remix or force_new or initial_plan is not None or setup_from_image or preloaded_draft is not None)
    if callable(getattr(setup_window,"_image_setup_return",None)):
        setup_window.navigation.setCurrentRow(6)
        setup_window.setup_page.image_setup_return_panel.action_button.setFocus()
        return
    retained_return = getattr(setup_window, "_generation_setup_return", None)
    if callable(retained_return):
        retained_return()
        if requested_scope:
            dialog = getattr(host, "_generation_dialog", None)
            if dialog is not None and not dialog._closed:
                dialog.status.setText("The requested source or model was not applied. Finish, save, or close this generation workspace, then choose it again.")
        if not requested_scope:
            return
    dialog = getattr(host, "_generation_dialog", None)
    if dialog is not None and not dialog._closed:
        if not requested_scope:
            window = _setup_window(host)
            if window is not None and hasattr(window, "generation_page"):
                window.navigation.setCurrentRow(7)
                window.generation_page.show_workspace(dialog)
            else:
                dialog.show(); dialog.raise_(); dialog.activateWindow()
            if callable(on_opened):
                on_opened(dialog)
            return
        if setup_window is None or not hasattr(setup_window,"generation_page"):
            dialog.status.setText("The requested source or model was not applied. Finish, save, or close this generation workspace, then choose it again.")
            dialog.show(); dialog.raise_(); dialog.activateWindow()
            return
    if getattr(host, "_generation_source_task", None) is not None:
        QMessageBox.information(parent, "Generation source is being prepared",
            "Wait for the selected source or model to finish preparing before opening another generation workspace.")
        return
    state = host.state
    settings = state.settings
    selected = tuple(dict.fromkeys(Path(path) for path in paths))
    if setup_from_image and len(selected) != 1:
        QMessageBox.information(parent, "Choose one image", "Choose one image to make a reusable Generation Setup.")
        return
    if record is not None:
        if record.modified:
            QMessageBox.information(parent, "Save Image Knowledge", "Save current image changes before planning from its generation reference.")
            return
        selected = (record.image_path,)
    if len(selected) > 100:
        QMessageBox.information(parent, "Choose a smaller planning scope", "Choose up to 100 images for one prompt plan.")
        return

    def current_source():
        # Local Only (including unclassified local paths) permits local work.
        # Thumbnail blur is also presentation, not generation permission.
        # Sharing remains subject to the separate Ingest transfer review;
        # execution admits only a loopback generator with a managed local backend.
        if initial_plan is not None:
            provider = (source_context or {}).get("current_provider")
            if initial_plan.source_identity and not callable(provider):
                raise ValueError("The original source must be reattached before repeating this request.")
            return provider() if callable(provider) else {}
        verifier = (source_context or {}).get("verified_source")
        if verifier is not None:
            admitted = {Path(path).resolve() for path in verifier()}
            if not selected or any(path.resolve() not in admitted for path in selected):
                raise ValueError("The selected finishing image is no longer a verified generation result.")
        return _source_identity(selected,
            frame_reference_provider=(source_context or {}).get("frame_reference_provider"))

    def source_image():
        if initial_plan is not None:
            provider = (source_context or {}).get("image_provider")
            if not callable(provider):
                raise ValueError("The original source image is not attached.")
            return provider()
        if len(selected) != 1:
            raise ValueError("Choose exactly one image to Remix.")
        raw, metadata = read_stable_training_source(selected[0])
        return {"bytes": raw, "sha256": metadata["sha256"]}

    model_registry = ModelResourceRegistry(MODEL_RESOURCES_PATH)

    def prepare():
        if initial_plan is not None:
            if current_source() != initial_plan.source_identity:
                raise ValueError("The original source changed. Reopen it before repeating generation.")
            return (initial_plan.source_identity, initial_plan.source, initial_plan.knowledge_fragments,
                    None, (source_context or {}).get("image_bytes"), "", ())
        identity = current_source() if selected else {}
        records = [ImageRecord(path) for path in selected]
        # A collection supplies selectable knowledge to a new goal. It never
        # silently chooses one member's generation request as the collection.
        saved_records = tuple(records[0].annotation.generation_records) if len(records) == 1 else ()
        source = saved_records[0] if saved_records and not setup_from_image else None
        fragments = []
        for index, r in enumerate(records):
            values = [(value, "Tag") for value in r.annotation.sdxl_tags]
            values.extend((value, "Description") for value in r.annotation.plain_language)
            for number, (value, kind) in enumerate(values[:200]):
                fragments.append({"id": f"image-{index}-{number}", "text": value, "group": "prompt_fragment",
                    "kind": kind,
                    "reviewed": True, "included": False, "scope": f"selected image {index + 1}",
                    "source": "reviewed_image_knowledge", "provenance": identity["collection"][index]})
        resource, resource_label = _registered_resource_with_label(model_registry, resource_id,
            selected_role=selected_role, selected_family=selected_family) if resource_id else (None, "")
        image_bytes = None
        if remix:
            from core.generation_inputs import freeze_init_image
            image = source_image()
            freeze_init_image(lambda: image, identity["collection"][0]["image_sha256"])
            image_bytes = image["bytes"]
        return identity, source, fragments, resource, image_bytes, resource_label, saved_records

    def show(result):
        from .generation_dialog import GenerationDialog
        from core.model_selection import protected_model_roots
        identity, source, fragments, resource, image_bytes, resource_label, saved_records = result
        character_db = getattr(state, "characters", None)
        characters = [c.to_dict() for c in getattr(character_db, "characters", {}).values()][:1000]
        outfits = [{**o.to_dict(), "associated_character_ids": list(character_db.associated_character_ids(o.id)),
                    "text": ", ".join(o.sdxl_tags), "reviewed": True}
                   for o in getattr(character_db, "outfits", {}).values()][:1000]
        endpoint, connection_issue = _saved_runtime_connection(setup_window)
        dialog = GenerationDialog(state.jobs, registry=model_registry,
            storage_root=USER_DATA_DIR / "generation", source_record=source,
            source_identity=identity, knowledge_fragments=fragments,
            source_current_provider=current_source if selected or initial_plan is not None else None,
            source_image_provider=source_image if remix or (initial_plan is not None and (source_context or {}).get("image_provider")) else None,
            source_image_bytes=image_bytes,
            protected_model_roots=protected_model_roots(settings),
            settings=settings,
            ingest_history=getattr(state, "moves", None),
            setup_available=setup_window is not None,
            initial_endpoint=endpoint, connection_issue=connection_issue,
            prompt_knowledge=getattr(state, "knowledge", None),
            characters=characters, outfits=outfits, parent=host)
        dialog.source_records = saved_records
        if preloaded_draft is not None:
            plan, target, token = preloaded_draft
            try:
                dialog._apply_loaded_plan(plan, target, token)
            except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
                dialog.deleteLater()
                QMessageBox.information(parent, "Image draft needs attention",
                    "The selected draft could not be resumed. Existing work was not changed. "
                    f"Choose Resume Image Draft again after checking the file.\n\n{exc}")
                return
        if setup_from_image and selected:
            # The path is used only after the user explicitly requests local
            # proposal-only analysis in the image setup editor.
            dialog.setup_suggestion_source = selected[0]
        if remix:
            dialog.configure_remix()
            dialog.image_task.setCurrentIndex(dialog.image_task.findData(initial_task))
            if initial_image_intent is not None:
                dialog.apply_image_origin_intent(initial_image_intent)
            if repair_context is not None:
                context = dict(repair_context)
                target = dict(dialog.plan.target_context)
                target["repair_context"] = context
                changes = {"target_context": target}
                if not dialog.plan.goal:
                    changes["goal"] = "Repair image from Problems" + (": " + context["reason"] if context["reason"] else "")
                dialog.plan = dialog.plan.revise(changes, source="repair_context",
                    consequence="Recorded problem context retained; original and recovery state unchanged")
                dialog._refresh_from_plan("Repair candidate from Problems. Choose how much to change and describe the correction in the prompt. "
                                          "Generated candidates return to Ingest unreviewed; the original stays in Problems.")
        if initial_plan is not None:
            dialog.plan = initial_plan
            dialog.remix_source = any(item.kind in {"init_image", "reference_image", "control_image"} for stage in initial_plan.request.stages for item in stage.inputs)
            if (source_context or {}).get("additional_references"):
                dialog._additional_references = deepcopy(source_context["additional_references"])
                dialog._primary_source_identity = deepcopy(source_context["primary_identity"])
                dialog._primary_current_provider = source_context["primary_current_provider"]
                dialog.source_current_provider = dialog._reference_current
            dialog._refresh_from_plan("Created an independent iteration. Review its seed and prepare it before submitting.")
        if setup_window is not None:
            _connect_setup_return(setup_window, dialog)
        if resource is not None:
            resources = [item.to_dict() for item in dialog.plan.request.resources
                         if item.hashes.get("sha256") != resource.hashes["sha256"]]
            resources.append(resource.to_dict())
            changes = {"resources": resources}
            changes["target_context"] = _updated_resource_labels(dialog.plan.target_context,
                resources, selected=resource.to_dict(), label=resource_label)
            if resource.role == "checkpoint":
                changes["model_hash"] = resource.hashes["sha256"]
            dialog.plan = dialog.plan.revise(changes, source="registered_model",
                consequence="Selected registered resource identity, role and lineage")
            dialog._refresh_from_plan()
        ingest_admitted = False
        ingest_pending = False
        def ingest(paths):
            nonlocal ingest_admitted, ingest_pending
            if ingest_admitted or ingest_pending or dialog._closed:
                return
            window = host
            while window.parent() is not None and not hasattr(window, "navigation"):
                window = window.parent()
            if not hasattr(window, "navigation"):
                dialog.status.setText("Ingest is unavailable. Keep this verified result and retry Send Verified Results to Ingest when Ingest is open.")
                QMessageBox.information(parent, "Add results through Ingest", "Open Ingest and use Add Images for the verified result shown in Generation History.")
                return
            previous_row = window.navigation.currentRow()
            expected_receipt = deepcopy(dialog.receipt)
            selected_paths = tuple(Path(path) for path in paths)
            ingest_pending = True
            window.navigation.setCurrentRow(1)
            def start_preview():
                nonlocal ingest_admitted, ingest_pending
                if dialog._closed:
                    ingest_pending = False
                    return
                if dialog.receipt != expected_receipt:
                    ingest_pending = False
                    if window.navigation.currentRow() == 1 and previous_row >= 0:
                        window.navigation.setCurrentRow(previous_row)
                    dialog.status.setText("The selected verified result changed before Ingest could open. Review it and retry Send Verified Results to Ingest.")
                    return
                if not hasattr(window, "ingest_page") or window.navigation.currentRow() != 1:
                    ingest_pending = False
                    if window.navigation.currentRow() == 1 and previous_row >= 0:
                        window.navigation.setCurrentRow(previous_row)
                    dialog.status.setText("Ingest did not open. Retry Send Verified Results to Ingest; the verified result remains here.")
                    return
                failure = ""
                try:
                    admitted = window.ingest_page._start_ingest_import_preview(
                        "files", list(selected_paths), preserve_source=True)
                except Exception as exc:
                    admitted = False
                    failure = f"Ingest preview could not start: {exc}. The verified result remains here; retry Send Verified Results to Ingest."
                if not admitted:
                    ingest_pending = False
                    folder_button = getattr(window.ingest_page, "choose_ingest_folder_button", None)
                    folder_recovery = bool(folder_button is not None and folder_button.isVisible())
                    if window.navigation.currentRow() == 1 and previous_row >= 0:
                        window.navigation.setCurrentRow(previous_row)
                    dialog.set_ingest_folder_recovery(folder_recovery)
                    dialog.status.setText(failure or (
                        "Ingest needs an Incoming Images folder. Choose Ingest Folder in Results & Queue."
                        if folder_recovery else
                        "Ingest could not start this preview. Finish its current operation, then retry Send Verified Results to Ingest. The verified result remains here."))
                    return
                ingest_admitted = True
                ingest_pending = False
                # Only an admitted preview takes over. The retained job still
                # owns its output and remains recoverable from history.
                dialog.close()
            QTimer.singleShot(0, start_preview)
        dialog.ingest_requested.connect(ingest)
        def choose_ingest_folder():
            if dialog._closed or not dialog._ingest_folder_recovery or ingest_pending:
                return
            page = getattr(host, "ingest_page", None)
            settings = getattr(page, "settings", None)
            if settings is None:
                dialog.status.setText("Ingest settings are unavailable. Keep this result and retry after Ingest opens.")
                return
            try:
                from .folder_locations import open_folder_locations
                from .ingest_page import _configured_ingest_folder
                open_folder_locations(dialog, focus="ingest_directory", settings=settings,
                    focus_group_only=True)
                folder = _configured_ingest_folder(settings)
                ready = bool(folder is not None and folder.is_dir())
            except Exception as exc:
                dialog.status.setText(f"Ingest folder setup could not open: {exc}. The verified result remains here.")
                return
            dialog.set_ingest_folder_recovery(not ready)
            dialog.status.setText(
                "Incoming Images folder saved. Choose Send Verified Results to Ingest again for a fresh check."
                if ready else
                "No available Incoming Images folder is saved. Choose Ingest Folder to correct it; this result remains here.")
            (dialog.ingest_button if ready else dialog.ingest_folder_button).setFocus()
        dialog.ingest_folder_requested.connect(choose_ingest_folder)
        def result_action(value):
            if value["plan"] is not None:
                context = {"current_provider": dialog.source_current_provider,
                           "image_provider": dialog.source_image_provider, "image_bytes": dialog.source_image_bytes,
                           "additional_references": deepcopy(dialog._additional_references),
                           "primary_identity": deepcopy(dialog._primary_source_identity),
                           "primary_current_provider": dialog._primary_current_provider}
                open_generation_workspace(host, force_new=True, initial_plan=value["plan"],
                    source_context=context, on_opened=value.get("on_opened"))
            else:
                def finishing_opened(target):
                    _apply_finishing_draft(target, value)
                    if callable(value.get("on_opened")): value["on_opened"]()
                open_generation_workspace(host, paths=value["paths"], remix=True, force_new=True,
                    initial_task=value["action"], on_opened=finishing_opened,
                    source_context={"verified_source": value["verified_source"]} if value.get("verified_source") else None)
        dialog.result_action_requested.connect(result_action)
        def detached():
            if getattr(host, "_generation_dialog", None) is dialog:
                host._generation_dialog = None
        dialog.workspace_closed.connect(detached)
        # The application window owns the live workspace across page entry points.
        host._generation_dialog = dialog
        if (setup_window is not None and hasattr(setup_window, "navigation")
                and setup_window.navigation.count() > 7
                and not hasattr(setup_window, "generation_page")):
            # Image-context entries may arrive before the lazy Generate section
            # has ever been opened. Load its canonical home before attaching
            # this workspace instead of leaving an orphan floating dialog.
            setup_window.navigation.setCurrentRow(7)
        if setup_window is not None and hasattr(setup_window,"generation_page"):
            setup_window.navigation.setCurrentRow(7)
            if setup_window.navigation.currentRow()==7:
                setup_window.generation_page.add_workspace(dialog,
                    label=(f"{initial_task.title()}: {selected[0].name}" if remix and selected else
                           "Iteration" if initial_plan is not None else
                           f"Draft: {Path(preloaded_draft[1]).stem}" if preloaded_draft is not None else "New Image"),
                    task="remix" if remix else "new")
                if callable(on_opened):
                    on_opened(dialog)
                if setup_from_image:
                    QTimer.singleShot(0, dialog.start_setup_from_image)
        else:
            dialog.show()
            if callable(on_opened):
                on_opened(dialog)
            if setup_from_image:
                QTimer.singleShot(0, dialog.start_setup_from_image)
        if not force_new and not selected and not resource_id and initial_plan is None:
            dialog.load_history(on_open=True)

    if not selected and not resource_id and initial_plan is None:
        show(({}, None, [], None, None, "", ())); return
    from .workers import CoordinatedCallTask
    task = CoordinatedCallTask(state.jobs, "Prepare generation source context", lambda progress: prepare(), resource="io")
    host._generation_source_task = task
    def completed(result):
        host._generation_source_task = None
        show(result)
    def failed(error):
        host._generation_source_task = None
        QMessageBox.information(parent, "Generation planning unavailable", error)
    def cancelled():
        host._generation_source_task = None
        QMessageBox.information(parent, "Generation planning unavailable", "Source preparation was cancelled. Choose the source again when ready.")
    task.signals.complete.connect(completed)
    task.signals.failed.connect(failed)
    task.signals.cancelled.connect(cancelled)
    try:
        task.start()
    except Exception:
        host._generation_source_task = None
        raise
