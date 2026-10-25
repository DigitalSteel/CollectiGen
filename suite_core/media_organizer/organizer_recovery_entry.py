"""Recovery and browser-opening composition lane; prior candidate unchanged."""
from dataclasses import replace
from .organizer_workflow_corrections import compose_final_runtime
from .hosted_encyclopedia_overlay import CompositionBoundaryError


def with_dataset_recovery(window_type):
    from .encyclopedia_handoff_entry import with_encyclopedia_handoff_settlement
    class RecoveryMediaOrganizerWindow(with_encyclopedia_handoff_settlement(window_type)):
        def workspace_extra_commands(self, index):
            if index not in (0,2):return []
            from qt_app.command_bar import UiCommand
            commands=[]
            if index==2:commands.append(UiCommand("organizer:package-recovery","Review Package Receipt.","Dataset Plans",
                callback=self.review_dataset_package_receipt,context="Dataset",
                description="Read an exact interrupted package receipt. Publication requires a separate review."))
            entry=getattr(self,"_native_encyclopedia_route",None)
            if entry is None:entry=getattr(self,"_hosted_encyclopedia_entry",None)
            if entry is not None:
                commands.append(UiCommand("organizer:hosted-encyclopedia","Tag Encyclopedia","Knowledge",
                    callback=entry.open,enabled=entry.available,context="Library" if index==0 else "Dataset",
                    description="Browse the approved Tag catalog and find local examples in included libraries.",
                    reason="The enrolled library host is unavailable or still closing its example workers."))
            return commands

        def configure_encyclopedia_handoffs(self, *, adapter):
            from core.organizer_encyclopedia_admission import OrganizerCatalogAdmissionReader
            from .encyclopedia_handoff_entry import install_encyclopedia_handoff_receiver
            existing = getattr(self, "_encyclopedia_handoff_receiver", None)
            if existing is not None:
                existing._check_host()
                reader = existing.read_admitted_catalog
                if getattr(reader, "adapter", None) is not adapter:
                    retired=getattr(self,"_encyclopedia_handoff_retired_receiver",None)
                    old_adapter=getattr(reader,"adapter",None)
                    if (retired is not existing or existing._pending_actions
                            or getattr(old_adapter,"root",None)!=getattr(adapter,"root",None)
                            or getattr(old_adapter,"catalog_id",None)!=getattr(adapter,"catalog_id",None)
                            or old_adapter.directory_pin!=adapter.directory_pin):
                        raise CompositionBoundaryError("The original selected catalog adapter must be retained until drained retirement.")
                    replacement=OrganizerCatalogAdmissionReader(self.state,adapter=adapter)
                    replacement(self.state)  # Verify current saved binding before re-enrollment.
                    existing._check_host()
                    existing.read_admitted_catalog=replacement
                    del self._encyclopedia_handoff_retired_receiver
                    reader=replacement
                return install_encyclopedia_handoff_receiver(self, read_admitted_catalog=reader)
            reader = OrganizerCatalogAdmissionReader(self.state, adapter=adapter)
            return install_encyclopedia_handoff_receiver(self, read_admitted_catalog=reader)

        def configure_encyclopedia_browser_handoffs(self, browser, *, adapter):
            catalog_host = getattr(browser, "catalog_host", None)
            selected = getattr(catalog_host, "accepted_source", None)
            publication = getattr(browser, "publication", None)
            if (getattr(catalog_host, "window", None) is not browser
                    or selected is None or selected.kind != "native"
                    or selected.path != getattr(adapter, "root", None)
                    or getattr(selected, "directory_pin", None) is None
                    or publication is None
                    or publication.catalog_id != getattr(adapter, "catalog_id", None)
                    or publication.snapshot.source.kind != "effective_native"
                    or publication.snapshot.source.completeness != "complete"):
                raise CompositionBoundaryError("The original accepted native browser binding is required.")
            prior_receiver = getattr(self, "_encyclopedia_handoff_receiver", None)
            prior_reader = getattr(prior_receiver,"read_admitted_catalog",None)
            prior_retired = getattr(self,"_encyclopedia_handoff_retired_receiver",None)
            prior_sender = (catalog_host.organizer_receiver, catalog_host.organizer_adapter)
            receiver = self.configure_encyclopedia_handoffs(adapter=adapter)
            try:
                catalog_host.configure_organizer_handoffs(receiver=receiver, adapter=adapter)
            except Exception:
                if catalog_host.organizer_receiver is receiver:
                    catalog_host.organizer_receiver, catalog_host.organizer_adapter = prior_sender
                    browser._set_commands()
                if prior_receiver is None and getattr(self, "_encyclopedia_handoff_receiver", None) is receiver:
                    del self._encyclopedia_handoff_receiver
                elif prior_receiver is receiver:
                    receiver.read_admitted_catalog=prior_reader
                    if prior_retired is receiver:self._encyclopedia_handoff_retired_receiver=receiver
                raise
            return receiver

        def install_native_encyclopedia_route(self, *, settings_path=None):
            from .encyclopedia_native_entry import install_native_encyclopedia_route
            return install_native_encyclopedia_route(self, settings_path=settings_path)

        def configure_hosted_encyclopedia_entry(self, **enrollment):
            from .encyclopedia_entry import HostedEncyclopediaEntry
            if getattr(self,"_hosted_encyclopedia_entry",None) is not None:
                raise CompositionBoundaryError("The existing hosted entry must retain its original enrollment.")
            self.organizer._check_context()
            entry=HostedEncyclopediaEntry(self,**enrollment)
            self._hosted_encyclopedia_entry=entry
            controller=getattr(self,"command_controller",None)
            if controller is not None:controller.refresh()
            return entry

        def retire_hosted_encyclopedia_entry(self, expected):
            if getattr(self,"_hosted_encyclopedia_entry",None) is not expected:
                raise CompositionBoundaryError("Only the original hosted enrollment can be retired.")
            receiver=getattr(self,"_encyclopedia_handoff_receiver",None)
            if receiver is not None and receiver._pending_actions:return False
            if not expected.retire():return False
            if (receiver is not None and getattr(expected,"selected_original_adapter",None)
                    is getattr(receiver.read_admitted_catalog,"adapter",None)):
                self._encyclopedia_handoff_retired_receiver=receiver
            self._hosted_encyclopedia_entry=None
            controller=getattr(self,"command_controller",None)
            if controller is not None:controller.refresh()
            expected.deleteLater()
            return True

        def closeEvent(self,event):
            entry=getattr(self,"_hosted_encyclopedia_entry",None)
            if entry is not None and not entry.prepare_close(self):
                event.ignore();return
            super().closeEvent(event)
            if entry is not None:entry.finish_host_close(event.isAccepted())

        def review_dataset_package_receipt(self):
            self.organizer._check_context()
            self.navigation.setCurrentRow(2)
            if self.stack.currentIndex()!=2:return False
            from PySide6.QtWidgets import QFileDialog
            filename,_=QFileDialog.getOpenFileName(self,"Review Package Receipt","","Package receipt (*.json)")
            if not filename:return False
            from pathlib import Path
            return bool(self._queue_dataset_page_action(
                lambda:self.dataset_page._start_training_package_recovery_preview(Path(filename))))

        def open_dataset_package_recovery(self):
            self.organizer._check_context()
            self.navigation.setCurrentRow(2)
            if self.stack.currentIndex()!=2:
                self.statusBar().showMessage(
                    "Finish the current workspace choice, then reopen the exact package receipt. It remains held.",12000)
                return False
            def resume():
                route=getattr(self.dataset_page,"resume_training_package_recovery_draft",None)
                if not callable(route):
                    self.statusBar().showMessage(
                        "The Dataset recovery adapter is unavailable; the exact receipt remains held.",12000)
                    return False
                return route()
            return bool(self._queue_dataset_page_action(resume))

        def open_hosted_encyclopedia_browser(self, *, open_browser,
                                             admitted_publication, task_factory, post_to_gui,
                                             gallery_factory=None, selected_original_adapter=None):
            """This composition lane owns explicit browser opening and attachment.

            Caller supplies its approved existing opening/adoption route and
            Publication. No new catalog, cache, session, dispatcher or engine.
            """
            self.organizer._check_context()
            if not callable(open_browser):
                raise CompositionBoundaryError("An existing approved browser-opening route is required.")
            if gallery_factory is not None and not callable(gallery_factory):
                raise CompositionBoundaryError("An explicit hosted gallery adapter is required.")
            browser=open_browser()
            if selected_original_adapter is not None:
                if browser.publication is not admitted_publication:
                    raise CompositionBoundaryError("The original admitted browser publication must be retained.")
                if getattr(getattr(browser, "catalog_host", None), "window", None) is not browser:
                    raise CompositionBoundaryError("The original browser/catalog host pair is required.")
            prior_receiver = getattr(self, "_encyclopedia_handoff_receiver", None)
            prior_reader = getattr(prior_receiver,"read_admitted_catalog",None)
            prior_retired = getattr(self,"_encyclopedia_handoff_retired_receiver",None)
            catalog_host = getattr(browser, "catalog_host", None)
            prior_sender = ((catalog_host.organizer_receiver, catalog_host.organizer_adapter)
                            if selected_original_adapter is not None else None)
            attachment = None
            receiver = None
            try:
                attachment=self.attach_encyclopedia_examples(browser,
                    admitted_publication=admitted_publication,task_factory=task_factory,
                    post_to_gui=post_to_gui)
                if gallery_factory is not None:
                    gallery_factory(browser,attachment.binding,self.organizer)
                if selected_original_adapter is not None:
                    receiver = self.configure_encyclopedia_browser_handoffs(
                        browser, adapter=selected_original_adapter)
                browser.show()
                return attachment
            except Exception:
                if receiver is not None:
                    if catalog_host.organizer_receiver is receiver:
                        catalog_host.organizer_receiver, catalog_host.organizer_adapter = prior_sender
                        browser._set_commands()
                    if prior_receiver is None and getattr(self, "_encyclopedia_handoff_receiver", None) is receiver:
                        del self._encyclopedia_handoff_receiver
                    elif prior_receiver is receiver:
                        receiver.read_admitted_catalog=prior_reader
                        if prior_retired is receiver:self._encyclopedia_handoff_retired_receiver=receiver
                if attachment is not None:
                    attachment.detach()
                raise

    RecoveryMediaOrganizerWindow.__name__="RecoveryMediaOrganizerWindow"
    return RecoveryMediaOrganizerWindow


def compose_recovery_runtime(reviewed_runtime, **kwargs):
    def start_recovery(*, startup_notice=""):
        exports=vars(reviewed_runtime.window_exports)
        corrected_type=exports.get("TagManagerWindow")
        if not isinstance(corrected_type,type):
            raise CompositionBoundaryError("The corrected startup factory is required.")
        recovery_type=with_dataset_recovery(corrected_type)
        exports["TagManagerWindow"]=recovery_type
        try:
            return reviewed_runtime.start_application(startup_notice=startup_notice)
        finally:
            if exports.get("TagManagerWindow") is not recovery_type:
                raise CompositionBoundaryError("Recovery factory ownership changed; left untouched.")
            exports["TagManagerWindow"]=corrected_type
    return compose_final_runtime(replace(reviewed_runtime,start_application=start_recovery),**kwargs)
