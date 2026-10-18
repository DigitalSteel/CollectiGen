from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QEvent, QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core import (
    AI_EVALUATION_DECISION_LABELS,
    AI_EVALUATION_HALLUCINATION_LABELS,
    AI_EVALUATION_QUALITY_LABELS,
    AI_EVALUATION_SAFETY_LABELS,
    privacy_safe_ai_evaluation_summary,
)

from .gallery import ProtectedImageView, content_safety_policy
from .modal_feedback import FeedbackEnabledDialog


CONTENT_RATING_LABELS = {
    "unknown":"Unsure / not classified",
    "general":"General",
    "sensitive":"Sensitive",
    "questionable":"Questionable",
    "explicit":"Explicit",
}


class AIBaselineEvaluationDialog(FeedbackEnabledDialog):
    """Review installed-model results without persisting paths or outputs."""

    feedback_workflow = "Installed local AI evaluation"

    def __init__(self,paths,session: dict,settings,parent=None):
        super().__init__(parent)
        self.setWindowTitle("Evaluate Installed Local AI")
        self.setModal(True)
        self.paths=tuple(Path(value) for value in paths)
        self.session=session
        self.settings=settings
        self.models=tuple(session.get("models",()))
        sample_count=max(0,int(session.get("sample_count",0) or 0))
        if (
            not self.paths or len(self.paths)!=sample_count
            or not self.models
            or any(len(model.get("samples",()))!=sample_count for model in self.models)
        ):
            raise ValueError(
                "The installed AI evaluation no longer matches its selected images."
            )
        self.items=tuple(
            (model,sample)
            for model in self.models
            for sample in model.get("samples",())
        )
        self.current_index=0
        self.reviews={}
        self.sample_ratings={index:"unknown" for index in range(1,sample_count+1)}
        self._privacy_hidden=False
        self._loading=False
        self._content_safety_policy=content_safety_policy()

        layout=QVBoxLayout(self)
        intro=QLabel(
            "Review what each installed local model produced for the same small "
            "sample. Rate visible usefulness, material unsupported claims, and "
            "content-safety handling. The images and raw outputs remain only in "
            "this window; the copied summary contains aggregate ratings, timing, "
            "resource use, and model identities only."
        )
        intro.setWordWrap(True)
        intro.setAccessibleName("Installed local AI evaluation instructions")
        layout.addWidget(intro)

        self.position=QLabel()
        self.position.setObjectName("sectionTitle")
        self.position.setAccessibleName("Evaluation result position")
        layout.addWidget(self.position)

        main=QSplitter(Qt.Horizontal)
        self.preview=ProtectedImageView(
            policy=self._content_safety_policy,settings=settings,
        )
        self.preview.setAccessibleName("Protected installed AI evaluation image")
        main.addWidget(self.preview)

        result_panel=QWidget()
        result_layout=QVBoxLayout(result_panel)
        result_layout.setContentsMargins(0,0,0,0)
        self.model_status=QLabel()
        self.model_status.setWordWrap(True)
        self.model_status.setObjectName("mutedLabel")
        self.model_status.setAccessibleName("Model and measurement details")
        result_layout.addWidget(self.model_status)
        self.output=QTextEdit()
        self.output.setReadOnly(True)
        self.output.setAccessibleName("Temporary local model output")
        result_layout.addWidget(self.output,1)
        main.addWidget(result_panel)
        main.setStretchFactor(0,4)
        main.setStretchFactor(1,6)
        main.setSizes([480,720])
        layout.addWidget(main,1)

        review=QGroupBox("Your Review")
        review_grid=QGridLayout(review)
        review_grid.addWidget(QLabel("Image content rating"),0,0)
        self.content_rating=QComboBox()
        for key,label in CONTENT_RATING_LABELS.items():
            self.content_rating.addItem(label,key)
        self.content_rating.setAccessibleName("Expected image content rating")
        review_grid.addWidget(self.content_rating,0,1)

        review_grid.addWidget(QLabel("Output quality"),0,2)
        self.quality=QComboBox()
        self.quality.addItem("Choose a rating…",0)
        for score,label in AI_EVALUATION_QUALITY_LABELS.items():
            self.quality.addItem(label,score)
        self.quality.setAccessibleName("Model output quality rating")
        review_grid.addWidget(self.quality,0,3)

        review_grid.addWidget(QLabel("Safety handling"),0,4)
        self.safety=QComboBox()
        self.safety.addItem("Choose an assessment…","")
        for key,label in AI_EVALUATION_SAFETY_LABELS.items():
            self.safety.addItem(label,key)
        self.safety.setAccessibleName("Model output content safety assessment")
        review_grid.addWidget(self.safety,0,5)

        hallucination_label=QLabel("Material unsupported claims")
        hallucination_label.setWordWrap(True)
        review_grid.addWidget(hallucination_label,1,0)
        hallucinations=QWidget()
        hallucination_layout=QHBoxLayout(hallucinations)
        hallucination_layout.setContentsMargins(0,0,0,0)
        hallucination_layout.setSpacing(8)
        self.hallucination_checks={}
        for key,label in AI_EVALUATION_HALLUCINATION_LABELS.items():
            control=QCheckBox(label)
            control.setAccessibleName(f"Unsupported claim: {label}")
            control.toggled.connect(
                lambda checked,value=key:self._hallucination_toggled(value,checked)
            )
            hallucination_layout.addWidget(control)
            self.hallucination_checks[key]=control
        hallucination_layout.addStretch(1)
        review_grid.addWidget(hallucinations,1,1,1,5)
        review_grid.setColumnStretch(1,1)
        review_grid.setColumnStretch(3,1)
        review_grid.setColumnStretch(5,1)
        layout.addWidget(review)

        decision_row=QHBoxLayout()
        decision_row.addWidget(QLabel("Next decision"))
        self.decision=QComboBox()
        for key,label in AI_EVALUATION_DECISION_LABELS.items():
            self.decision.addItem(label,key)
        self.decision.setAccessibleName("Installed AI evaluation decision")
        decision_row.addWidget(self.decision,1)
        self.review_status=QLabel()
        self.review_status.setObjectName("mutedLabel")
        self.review_status.setAccessibleName("Evaluation review completion")
        decision_row.addWidget(self.review_status)
        layout.addLayout(decision_row)

        navigation=QHBoxLayout()
        self.previous_button=QPushButton("Previous")
        self.previous_button.clicked.connect(lambda:self._move(-1))
        self.next_button=QPushButton("Next")
        self.next_button.clicked.connect(lambda:self._move(1))
        self.next_unrated_button=QPushButton("Next Unrated")
        self.next_unrated_button.clicked.connect(self._next_unrated)
        self.copy_button=QPushButton("Copy Summary")
        self.copy_button.setProperty("primary",True)
        self.copy_button.setToolTip(
            "Copy aggregate scores and measurements without image paths, pixels, or raw outputs."
        )
        self.copy_button.setAccessibleDescription(self.copy_button.toolTip())
        self.copy_button.clicked.connect(self.copy_summary)
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.next_button)
        navigation.addWidget(self.next_unrated_button)
        navigation.addStretch(1)
        navigation.addWidget(self.copy_button)
        layout.addLayout(navigation)

        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.content_rating.currentIndexChanged.connect(self._review_changed)
        self.quality.currentIndexChanged.connect(self._review_changed)
        self.safety.currentIndexChanged.connect(self._review_changed)
        self.resize(1320,820)
        self._load_current()

    @staticmethod
    def _review_key(model: dict,sample: dict) -> tuple[str,int]:
        return (
            str(model.get("model_id") or ""),
            max(1,int(sample.get("sample_number",0) or 0)),
        )

    def _current(self) -> tuple[dict,dict]:
        return self.items[self.current_index]

    def _hallucination_toggled(self,key: str,checked: bool) -> None:
        if self._loading or not checked:
            self._review_changed()
            return
        if key=="none":
            for other_key,control in self.hallucination_checks.items():
                if other_key!="none" and control.isChecked():
                    blocker=QSignalBlocker(control)
                    control.setChecked(False)
                    del blocker
        else:
            none=self.hallucination_checks["none"]
            if none.isChecked():
                blocker=QSignalBlocker(none)
                none.setChecked(False)
                del blocker
        self._review_changed()

    def _review_changed(self,*_args) -> None:
        if self._loading:
            return
        self._save_current()
        self._refresh_completion()

    def _save_current(self) -> None:
        model,sample=self._current()
        sample_number=max(1,int(sample.get("sample_number",0) or 0))
        self.sample_ratings[sample_number]=str(
            self.content_rating.currentData() or "unknown"
        )
        hallucinations=tuple(sorted(
            key for key,control in self.hallucination_checks.items()
            if control.isChecked()
        ))
        quality=int(self.quality.currentData() or 0)
        safety=str(self.safety.currentData() or "")
        key=self._review_key(model,sample)
        if quality and safety and hallucinations:
            self.reviews[key]={
                "quality":quality,
                "safety":safety,
                "hallucinations":hallucinations,
            }
        else:
            self.reviews.pop(key,None)

    def _load_current(self) -> None:
        self._loading=True
        try:
            model,sample=self._current()
            sample_number=max(1,int(sample.get("sample_number",0) or 0))
            model_number=self.models.index(model)+1
            self.position.setText(
                f"Model {model_number:,} of {len(self.models):,} • "
                f"Sample {sample_number:,} of {len(self.paths):,}"
            )
            self.preview.policy=self._content_safety_policy
            self.preview.set_path(self.paths[sample_number-1])
            timings=[float(item.get("seconds",0) or 0) for item in model.get("samples",())]
            resources=model.get("resources",{}) if isinstance(model.get("resources"),dict) else {}
            peak=resources.get("peak",{}) if isinstance(resources.get("peak"),dict) else {}
            gpu=peak.get("gpu_process_bytes")
            rss=peak.get("process_rss_bytes")
            device=model.get("device",{}) if isinstance(model.get("device"),dict) else {}
            detail=(
                f"{model.get('display_name') or model.get('model_id')} • "
                f"{float(sample.get('seconds',0) or 0):.2f}s for this image"
            )
            if device.get("name") or device.get("provider") or device.get("kind"):
                detail+=f" • {device.get('name') or device.get('provider') or str(device.get('kind')).upper()}"
            if gpu is not None:
                detail+=f" • peak GPU process {int(gpu)/(1024**2):,.0f} MiB"
            if rss is not None:
                detail+=f" • peak app memory {int(rss)/(1024**2):,.0f} MiB"
            if len(timings)>1:
                detail+=f" • warm range {min(timings[1:]):.2f}–{max(timings[1:]):.2f}s"
            self.model_status.setText(detail)
            output=sample.get("output",{}) if isinstance(sample.get("output"),dict) else {}
            if model.get("kind")=="sdxl_tags":
                rows=[]
                for item in output.get("tags",()):
                    category=str(item.get("category") or "general").title()
                    rows.append(
                        f"{item.get('tag')}  •  {float(item.get('confidence',0) or 0):.3f}  •  {category}"
                    )
                text="\n".join(rows) or "[No tags met the saved thresholds]"
            else:
                text=str(output.get("plain_language") or "[No description returned]")
            self.output.setPlainText(text)
            review=self.reviews.get(self._review_key(model,sample),{})
            self.content_rating.setCurrentIndex(max(
                0,self.content_rating.findData(self.sample_ratings[sample_number]),
            ))
            self.quality.setCurrentIndex(max(
                0,self.quality.findData(int(review.get("quality",0) or 0)),
            ))
            self.safety.setCurrentIndex(max(
                0,self.safety.findData(str(review.get("safety") or "")),
            ))
            selected=set(review.get("hallucinations",()))
            for key,control in self.hallucination_checks.items():
                control.setChecked(key in selected)
            self.previous_button.setEnabled(self.current_index>0)
            self.next_button.setEnabled(self.current_index<len(self.items)-1)
            self._privacy_hidden=False
        finally:
            self._loading=False
        self._refresh_completion()

    def _move(self,offset: int) -> None:
        self._save_current()
        target=max(0,min(len(self.items)-1,self.current_index+int(offset)))
        if target==self.current_index:
            return
        self.current_index=target
        self._load_current()

    def _next_unrated(self) -> None:
        self._save_current()
        for offset in range(1,len(self.items)+1):
            target=(self.current_index+offset)%len(self.items)
            model,sample=self.items[target]
            if self._review_key(model,sample) not in self.reviews:
                self.current_index=target
                self._load_current()
                return

    def _refresh_completion(self) -> None:
        completed=len(self.reviews)
        total=len(self.items)
        self.review_status.setText(f"Reviewed {completed:,} of {total:,}")
        self.review_status.setAccessibleDescription(
            f"{completed:,} of {total:,} model results have complete ratings."
        )
        ready=completed==total
        self.copy_button.setEnabled(ready)
        self.next_unrated_button.setEnabled(not ready)

    def privacy_safe_summary(self) -> dict:
        self._save_current()
        return privacy_safe_ai_evaluation_summary(
            self.session,self.reviews,self.sample_ratings,
            str(self.decision.currentData() or "keep_baseline"),
        )

    def copy_summary(self) -> None:
        try:
            summary=self.privacy_safe_summary()
        except ValueError as exc:
            self.review_status.setText(str(exc))
            return
        QApplication.clipboard().setText(
            json.dumps(summary,indent=2,sort_keys=True)+"\n"
        )
        from .components import window_status_footer
        window_status_footer(self.window()).show_message("Privacy-safe summary copied")

    def prepare_for_privacy_capture(self) -> None:
        self.preview.prepare_for_privacy_capture()
        self.output.setPlainText("[Model output hidden for privacy capture]")
        self._privacy_hidden=True

    def discard_sensitive_state(self) -> None:
        self._privacy_hidden=False
        self.preview.clear("Evaluation preview discarded")
        self.output.clear()
        self.paths=()
        self.session={}
        self.models=()
        self.items=()
        self.reviews={}
        self.sample_ratings={}

    def done(self,result: int) -> None:
        self.discard_sensitive_state()
        super().done(result)

    def closeEvent(self,event) -> None:
        self.discard_sensitive_state()
        super().closeEvent(event)

    def changeEvent(self,event) -> None:
        super().changeEvent(event)
        if (
            event.type()==QEvent.ActivationChange
            and self.isActiveWindow()
            and self._privacy_hidden
            and self.items
        ):
            self._load_current()
