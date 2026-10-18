from __future__ import annotations

from PySide6.QtCore import QSize,QThreadPool,Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,QHeaderView,QHBoxLayout,QLabel,QPlainTextEdit,QPushButton,QTreeWidget,
    QTreeWidgetItem,QVBoxLayout,QWidget,
)

from core import (
    DependencyService,JOYCAPTION_MODEL,PLAIN_LANGUAGE_MODELS,WD_TAGGER_DESCRIPTIONS,
    WD_TAGGER_MODELS,
)
from .components import DisclosureButton,compact_button
from .modal_feedback import FeedbackEnabledDialog
from .workers import AsyncCallTask


class CapabilityManagerDialog(FeedbackEnabledDialog):
    """Plain-language per-component install and uninstall choices."""

    feedback_workflow = "Installed AI component management"

    def __init__(
        self,service: DependencyService,capability: str,parent=None,*,
        focus_model_id: str="",runtime_observation=None,
    ):
        super().__init__(parent)
        if capability not in {"wd","joy"}:
            raise ValueError("Only optional AI capabilities can be managed.")
        self.service=service; self.capability=capability
        self.focus_model_id=focus_model_id
        self.requested_action: tuple[str,str]=("","")
        capability_label="AI Tags" if capability=="wd" else "AI Descriptions"
        self.setWindowTitle(f"Manage {capability_label}")
        self.resize(1120,620); self.setMinimumSize(860,500)
        layout=QVBoxLayout(self)
        title=QLabel(f"Manage {capability_label}")
        title.setObjectName("pageTitle"); layout.addWidget(title)
        intro=QLabel(
            "View each supported local component and choose an action. Models "
            "can be installed or uninstalled independently. The shared runtime "
            "stays grouped so its required packages remain compatible."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel")
        layout.addWidget(intro)

        self.runtime_check=runtime_observation if capability=="wd" else None
        self.runtime_check_task=None
        if capability=="wd":
            check_row=QHBoxLayout()
            self.runtime_check_status=QLabel(
                runtime_observation.summary.replace(
                    "Open Manage AI Tags to ","Choose ",
                ) if runtime_observation is not None else
                "Runtime files may be installed without a working native load. "
                "Check this Python environment before using AI Tags."
            )
            self.runtime_check_status.setWordWrap(True)
            self.runtime_check_status.setObjectName("mutedLabel")
            self.runtime_check_button=compact_button(QPushButton("Check Runtime"))
            self.runtime_check_button.setAccessibleDescription(
                "Read-only ONNX Runtime import check in a separate Python process; "
                "does not open images or run a model."
            )
            self.runtime_check_button.setToolTip(
                self.runtime_check_button.accessibleDescription()
            )
            self.runtime_check_button.clicked.connect(self._check_runtime)
            check_row.addWidget(self.runtime_check_status,1)
            check_row.addWidget(self.runtime_check_button)
            layout.addLayout(check_row)

        self.table=QTreeWidget()
        self.table.setHeaderLabels(("Component","Status","What it provides","Action"))
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setUniformRowHeights(False)
        self.table.setAccessibleName(f"{capability_label} installed components")
        header=self.table.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0,QHeaderView.Fixed)
        header.setSectionResizeMode(1,QHeaderView.Fixed)
        header.setSectionResizeMode(2,QHeaderView.Stretch)
        header.setSectionResizeMode(3,QHeaderView.Fixed)
        header.resizeSection(0,190)
        header.resizeSection(1,115)
        header.resizeSection(3,240)
        layout.addWidget(self.table,1)

        self._add_runtime_row()
        self._add_model_rows()
        if capability=="wd":
            installed=self._runtime_state()[1]
            self.runtime_check_status.setVisible(installed)
            self.runtime_check_button.setVisible(installed)
            if runtime_observation is not None:
                self.runtime_row.setText(
                    1,"Runtime loads" if runtime_observation.loaded else "Needs repair",
                )
                self.runtime_row.setToolTip(1,runtime_observation.summary)
        if self.table.currentItem() is not None:
            self.table.scrollToItem(self.table.currentItem())

        technical=QWidget(); technical_layout=QVBoxLayout(technical)
        technical_layout.setContentsMargins(0,0,0,0)
        self.technical_text=QPlainTextEdit(self._technical_details())
        self.technical_text.setReadOnly(True)
        self.technical_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.technical_text.setMaximumHeight(220)
        self.technical_text.setAccessibleName("Exact installed component details")
        technical_layout.addWidget(self.technical_text)
        self.technical_toggle=DisclosureButton(
            "Show Technical Details…","Hide Technical Details",technical,
        )
        layout.addWidget(self.technical_toggle,0,Qt.AlignLeft)
        layout.addWidget(technical)

        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def _runtime_names(self) -> tuple[str,...]:
        if self.capability=="wd":
            names=set(DependencyService.WD_PACKAGES)
        else:
            names=set(DependencyService.JOY_BASE_PACKAGES)
            if (
                str(getattr(
                    self.service.settings,"plain_language_model","",
                ))==JOYCAPTION_MODEL
                and str(getattr(
                    self.service.settings,"joycaption_quantization","bf16",
                ))=="nf4"
            ):
                names.add("BitsAndBytes")
        return tuple(sorted(names,key=str.casefold))

    def _runtime_state(self) -> tuple[str,bool,bool,str]:
        components=[self.service.component(name) for name in self._runtime_names()]
        installed=sum(bool(component and component.installed) for component in components)
        hardware_ready=True
        if self.capability=="joy":
            hardware=self.service.component(DependencyService.JOY_HARDWARE_COMPONENT)
            hardware_ready=bool(hardware and hardware.installed)
        if installed==len(components):
            return "Installed",True,True,"Required by every model listed below."
        if not hardware_ready:
            return "Unavailable",False,False,"A supported NVIDIA GPU is required."
        if installed:
            return "Needs repair",False,True,"Some required runtime files are missing."
        return "Not installed",False,True,"Required by every model listed below."

    def _add_action_row(
        self,label: str,status: str,summary: str,action: str,identity: str,
        *,enabled: bool=True,
    ) -> QTreeWidgetItem:
        item=QTreeWidgetItem((label,status,summary,""))
        item.setToolTip(0,label); item.setToolTip(1,status); item.setToolTip(2,summary)
        self.table.addTopLevelItem(item)
        button=compact_button(QPushButton(action))
        button.setEnabled(enabled)
        button.setAccessibleName(f"{action.removesuffix('…')} {label}")
        button.setAccessibleDescription(f"{action.removesuffix('…')} the {label} component.")
        button.clicked.connect(
            lambda _checked=False,value=identity:self._choose(value),
        )
        self.table.setItemWidget(item,3,button)
        item.setSizeHint(0,QSize(0,button.sizeHint().height()+8))
        return item

    def _add_runtime_row(self) -> None:
        status,installed,enabled,summary=self._runtime_state()
        label="Shared AI Tag runtime" if self.capability=="wd" else "Shared AI Description runtime"
        hardware=(self.service.component(DependencyService.JOY_HARDWARE_COMPONENT)
                  if self.capability=="joy" else None)
        can_repair=installed and (self.capability=="wd" or bool(hardware and hardware.installed))
        action=("Repair…" if can_repair or status=="Needs repair" else
                "Uninstall…" if installed else "Install…")
        item=self._add_action_row(
            label,status,summary,action,
            "install_runtime" if can_repair or not installed else "uninstall_runtime",
            enabled=enabled,
        )
        self.runtime_row=item
        if can_repair:
            repair=compact_button(QPushButton("Repair…"),primary=True)
            repair.setAccessibleName(f"Repair {label}")
            repair.setAccessibleDescription(
                "Reinstall the reviewed shared runtime after confirmation. "
                "Installed model files remain separate."
            )
            repair.clicked.connect(lambda:self._choose("install_runtime"))
            uninstall=compact_button(QPushButton("Uninstall…"))
            uninstall.setAccessibleName(f"Uninstall {label}")
            uninstall.clicked.connect(lambda:self._choose("uninstall_runtime"))
            actions=QWidget(); row=QHBoxLayout(actions)
            row.setContentsMargins(0,0,0,0); row.setSpacing(5)
            row.addWidget(repair); row.addWidget(uninstall)
            self.table.setItemWidget(item,3,actions)
            item.setSizeHint(0,QSize(0,actions.sizeHint().height()+8))

    def _check_runtime(self) -> None:
        if self.runtime_check_task is not None:
            return
        self.runtime_check_button.setEnabled(False)
        self.runtime_check_status.setText("Checking native AI Tag runtime in this Python environment…")
        task=AsyncCallTask(self.service.check_wd_runtime_import)
        self.runtime_check_task=task
        task.signals.complete.connect(self._runtime_check_complete)
        task.signals.failed.connect(self._runtime_check_failed)
        QThreadPool.globalInstance().start(task,-1)

    def _runtime_check_complete(self,result) -> None:
        self.runtime_check_task=None
        self.runtime_check=result
        self.runtime_check_button.setEnabled(True)
        self.runtime_check_button.setText("Check Again")
        self.runtime_check_status.setText(result.summary)
        self.runtime_row.setText(1,"Runtime loads" if result.loaded else "Needs repair")
        self.runtime_row.setToolTip(1,result.summary)

    def _runtime_check_failed(self,_message: str) -> None:
        self.runtime_check_task=None
        self.runtime_check_button.setEnabled(True)
        self.runtime_check_button.setText("Check Again")
        self.runtime_check_status.setText(
            "The runtime check did not finish. Check again or repair the shared AI Tag runtime."
        )
        self.runtime_row.setText(1,"Check failed")

    def _model_specs(self):
        if self.capability=="wd":
            for model_id,(display,_directory) in WD_TAGGER_MODELS.items():
                yield model_id,display,WD_TAGGER_DESCRIPTIONS[model_id]
        else:
            for model_id,(display,_directory,summary) in PLAIN_LANGUAGE_MODELS.items():
                yield model_id,display,summary

    def _add_model_rows(self) -> None:
        hardware_ready=True
        if self.capability=="joy":
            hardware=self.service.component(DependencyService.JOY_HARDWARE_COMPONENT)
            hardware_ready=bool(hardware and hardware.installed)
        for model_id,display,summary in self._model_specs():
            component_name=(
                self.service.wd_component_name(model_id)
                if self.capability=="wd" else
                self.service.plain_language_component_name(model_id)
            )
            component=self.service.component(component_name)
            installed=bool(component and component.installed)
            available=installed or hardware_ready
            if not available:
                summary+= " A supported NVIDIA GPU is required to install and run it."
            item=self._add_action_row(
                display,(
                    "Installed" if installed else
                    "Not installed" if available else "Unavailable"
                ),summary,
                "Uninstall…" if installed else "Install…",
                ("uninstall_model:" if installed else "install_model:")+model_id,
                enabled=available,
            )
            if model_id==self.focus_model_id:
                self.table.setCurrentItem(item)

    def _technical_details(self) -> str:
        lines=["SHARED RUNTIME"]
        for name in self._runtime_names():
            component=self.service.component(name)
            ready=bool(component and component.installed)
            version=f" · {component.version}" if component and component.version else ""
            location=f"\n  {component.location}" if component and component.location else ""
            lines.append(f"{'Installed' if ready else 'Missing'} · {name}{version}{location}")
        lines.extend(("","MODELS"))
        for model_id,display,_summary in self._model_specs():
            component_name=(
                self.service.wd_component_name(model_id)
                if self.capability=="wd" else
                self.service.plain_language_component_name(model_id)
            )
            component=self.service.component(component_name)
            ready=bool(component and component.installed)
            location=f"\n  {component.location}" if component and component.location else ""
            revision=f"\n  Revision: {component.version}" if component and component.version else ""
            lines.append(f"{'Installed' if ready else 'Missing'} · {display}{revision}{location}")
        return "\n".join(lines)

    def _choose(self,identity: str) -> None:
        if ":" in identity:
            action,value=identity.split(":",1)
        else:
            action,value=identity,""
        self.requested_action=(action,value)
        self.accept()
