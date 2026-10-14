"""Deliberate normal Organizer command using the existing snapshot reader."""


class NativeEncyclopediaRoute:
    def __init__(self,host, *, settings_path=None):
        from pathlib import Path
        if settings_path is not None:
            settings_path = Path(settings_path)
            if not settings_path.is_absolute() or ".." in settings_path.parts:
                raise ValueError("An explicitly admitted absolute Encyclopedia settings path is required")
        self.host=host
        self.settings_path=settings_path
        self.browser=None

    def available(self):
        try:
            self.host.organizer._check_context()
            jobs = self.host.state.jobs
            return (not getattr(jobs,"shutting_down",False)
                    and callable(getattr(jobs,"create_execution_scope",None)))
        except Exception:
            return False

    def open(self):
        if not self.available():return False
        if self.settings_path is not None:
            from PySide6.QtWidgets import QInputDialog
            choices=("Browse a native Tag Database", "Open an approved snapshot")
            choice,confirmed=QInputDialog.getItem(self.host,"Tag Encyclopedia", "Choose what to open:",choices,0,False)
            if not confirmed:return False
            if choice==choices[0]:return self._open_native_browser()
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QFileDialog
        from qt_app.workers import CoordinatedCallTask
        from suite_core.encyclopedia.browser import EncyclopediaWindow
        filename,_=QFileDialog.getOpenFileName(self.host,"Open approved Tag Encyclopedia snapshot","",
            "Snapshot JSON (*.json);;All files (*)")
        if not filename:return False
        previous=getattr(self.host,"_hosted_encyclopedia_entry",None)
        if previous is not None and not self.host.retire_hosted_encyclopedia_entry(previous):
            self.host.statusBar().showMessage("The original example workers are still closing. Wait for Close to finish, then choose Tag Encyclopedia again to reconnect.",12000)
            return False
        browser=EncyclopediaWindow()
        if not browser.open_snapshot(filename):
            self.host.statusBar().showMessage("The selected snapshot could not be opened. Your library is unchanged.",12000)
            browser.close();browser.deleteLater();return False
        # Source choice is deliberate. The original hosted binding independently
        # verifies each requested native identity/category and included scope.
        # No catalog capture, publishing, store or default-library search here.
        entry=None
        try:
            entry=self.host.configure_hosted_encyclopedia_entry(
                open_browser=lambda:browser,admitted_publication=browser.publication,
                task_factory=CoordinatedCallTask,
                post_to_gui=lambda callback:QTimer.singleShot(0,self.host,callback))
            if entry.open():return True
        except Exception:
            pass
        if entry is not None:
            if not self.host.retire_hosted_encyclopedia_entry(entry):
                self.host.statusBar().showMessage("The failed connection is still draining. Wait for Close, then choose Tag Encyclopedia again.",12000)
                return False
        self.host.statusBar().showMessage("Hosted examples could not connect. Choose Tag Encyclopedia again to select a snapshot; nothing was applied.",12000)
        browser.close();browser.deleteLater();return False

    def _open_native_browser(self):
        from suite_core.encyclopedia.__main__ import create_window
        from PySide6.QtCore import QTimer
        from qt_app.workers import CoordinatedCallTask
        if self.browser is not None and self.browser.isVisible():
            self.browser.raise_();self.browser.activateWindow();return True
        previous=getattr(self.host,"_hosted_encyclopedia_entry",None)
        if previous is not None and not self.host.retire_hosted_encyclopedia_entry(previous):
            self.host.statusBar().showMessage("The original example workers are still closing. Wait before reopening Tag Encyclopedia.",12000)
            return False
        browser=create_window(settings_path=self.settings_path)
        self.browser=browser
        catalog_host=browser.catalog_host
        def accepted(binding):
            if (self.browser is not browser or catalog_host.window is not browser
                    or catalog_host.accepted_source is not binding
                    or binding.kind != "native" or binding.native_adapter is None
                    or catalog_host.accepted_native_adapter is not binding.native_adapter
                    or browser.publication is not binding.publication):
                return
            adapter=binding.native_adapter
            try:
                self.host.organizer._check_context()
                # Fresh original saved authority must already match Organizer;
                # observation never adopts a catalog or replaces Knowledge.
                from core.organizer_encyclopedia_admission import OrganizerCatalogAdmissionReader
                OrganizerCatalogAdmissionReader(self.host.state,adapter=adapter)(self.host.state)
                entry=getattr(self.host,"_hosted_encyclopedia_entry",None)
                if entry is not None:
                    if entry.browser is not browser or entry.selected_original_adapter is not adapter:
                        raise ValueError("Retain the original hosted browser and selected adapter")
                    self.host.configure_encyclopedia_browser_handoffs(browser,adapter=adapter)
                    return
                entry=self.host.configure_hosted_encyclopedia_entry(
                    open_browser=lambda:browser,admitted_publication=binding.publication,
                    task_factory=CoordinatedCallTask,
                    post_to_gui=lambda callback:QTimer.singleShot(0,self.host,callback),
                    selected_original_adapter=adapter)
                if not entry.open():
                    self.host.retire_hosted_encyclopedia_entry(entry)
                    raise ValueError("Hosted browser attachment was held")
            except Exception:
                browser.status.setText("Organizer connection is held. Open the same admitted Tag Database in Organizer, then Refresh this native catalog. No image was changed.")
        catalog_host.catalog_accepted.connect(
            lambda binding: QTimer.singleShot(0,browser,lambda:accepted(binding)))
        browser.show()
        return True


def install_native_encyclopedia_route(host, *, settings_path=None):
    """Retain only a command adapter after native facade adoption; no data reads."""
    existing=getattr(host,"_native_encyclopedia_route",None)
    if existing is not None:
        if settings_path is None:return existing
        from pathlib import Path
        if existing.settings_path is not None and existing.settings_path != Path(settings_path):
            raise ValueError("Retain the original admitted Encyclopedia settings path")
        if existing.browser is not None:
            raise ValueError("Retain the original native browser enrollment")
        existing.settings_path=NativeEncyclopediaRoute(host,settings_path=settings_path).settings_path
        return existing
    if not callable(getattr(host.state.jobs,"create_execution_scope",None)):return
    host._native_encyclopedia_route=NativeEncyclopediaRoute(host,settings_path=settings_path)
    controller=getattr(host,"command_controller",None)
    if controller is not None:controller.refresh()
    return host._native_encyclopedia_route
