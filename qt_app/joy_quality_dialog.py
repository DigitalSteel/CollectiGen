from __future__ import annotations

import secrets
from pathlib import Path

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .gallery import ProtectedImageView, content_safety_policy
from .modal_feedback import FeedbackEnabledDialog


MODE_LABELS = {
    "bf16": "Full BF16",
    "nf4": "4-bit NF4",
}


class JoyCaptionQualityDialog(FeedbackEnabledDialog):
    """Review an in-memory, blinded JoyCaption quality comparison."""

    feedback_workflow = "Plain Language caption quality comparison"

    def __init__(
        self,
        paths,
        comparison: dict,
        settings,
        parent=None,
        *,
        mode_orders=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Compare Caption Quality")
        self.setModal(True)
        self.paths=tuple(Path(value) for value in paths)
        self.samples=tuple(comparison.get("samples",()))
        if not self.paths or len(self.paths)!=len(self.samples):
            raise ValueError(
                "The caption comparison no longer matches its selected images."
            )
        self.comparison_kind=str(
            comparison.get("comparison_kind") or "memory_mode"
        )
        configuration=comparison.get("configuration",{})
        if not isinstance(configuration,dict):
            configuration={}
        fallback_variants=configuration.get("compared_modes",("bf16","nf4"))
        self.variants=tuple(
            str(value) for value in comparison.get(
                "variants",fallback_variants,
            )
        )
        if len(self.variants)!=2 or self.variants[0]==self.variants[1]:
            raise ValueError(
                "The caption comparison must contain two distinct variants."
            )
        supplied_labels=comparison.get("variant_labels",{})
        self.variant_labels={
            variant:str(
                supplied_labels.get(
                    variant,MODE_LABELS.get(variant,variant.upper()),
                )
            )
            for variant in self.variants
        }
        self.settings=settings
        self._content_safety_policy=content_safety_policy()
        self.current_index=0
        self.revealed=set()
        self._privacy_hidden=False
        orders=list(mode_orders or ())
        while len(orders)<len(self.samples):
            orders.append(
                self.variants
                if secrets.randbelow(2)==0 else
                tuple(reversed(self.variants))
            )
        self.mode_orders=tuple(tuple(value) for value in orders[:len(self.samples)])
        if any(
            len(order)!=2 or set(order)!=set(self.variants)
            for order in self.mode_orders
        ):
            raise ValueError(
                "The blinded caption order does not match the comparison."
            )

        layout=QVBoxLayout(self)
        if self.comparison_kind=="token_limit":
            intro_text=(
                "Compare the wording, visible accuracy, and useful detail "
                "before revealing which output limit produced each caption. "
                "Both captions use the same saved model, instructions, beam "
                "count, and 4-bit NF4 memory mode; only the generated-token "
                "ceiling differs."
            )
            self._reveal_label="Reveal Limits"
            self._revealed_label="Limits Revealed"
        else:
            intro_text=(
                "Compare the wording and visible accuracy before revealing "
                "which caption used each memory mode. Both captions use the "
                "same saved model, instructions, beam count, and length "
                "controls; only BF16 versus NF4 differs."
            )
            self._reveal_label="Reveal Modes"
            self._revealed_label="Modes Revealed"
        intro=QLabel(intro_text)
        intro.setWordWrap(True)
        intro.setAccessibleName("Caption quality comparison instructions")
        layout.addWidget(intro)

        generation=(
            configuration.get("generation",{})
            if isinstance(configuration,dict) else {}
        )
        beams=max(1,int(generation.get("num_beams",1) or 1))
        if self.comparison_kind=="token_limit":
            limits=configuration.get("compared_token_limits",())
            limits_text=" versus ".join(
                f"{max(1,int(value)):,}" for value in limits
            )
            configuration_text=(
                f"Controlled settings • 4-bit NF4 • {beams:,} beam"
                f"{'s' if beams != 1 else ''} • {limits_text} generated-token "
                "ceilings • deterministic wording"
            )
        else:
            tokens=max(1,int(generation.get("max_new_tokens",1) or 1))
            configuration_text=(
                f"Controlled settings • {beams:,} beam"
                f"{'s' if beams != 1 else ''} • up to {tokens:,} generated "
                "tokens • deterministic wording"
            )
        self.configuration=QLabel(configuration_text)
        self.configuration.setObjectName("mutedLabel")
        self.configuration.setWordWrap(True)
        layout.addWidget(self.configuration)

        main=QSplitter(Qt.Horizontal)
        self.preview=ProtectedImageView(
            policy=self._content_safety_policy,settings=settings,
        )
        self.preview.setAccessibleName(
            "Protected image used for caption comparison"
        )
        main.addWidget(self.preview)

        captions=QWidget()
        captions_layout=QVBoxLayout(captions)
        captions_layout.setContentsMargins(0,0,0,0)
        self.sample_status=QLabel()
        self.sample_status.setObjectName("sectionTitle")
        self.sample_status.setAccessibleName("Comparison sample position")
        captions_layout.addWidget(self.sample_status)
        caption_splitter=QSplitter(Qt.Horizontal)
        self.caption_groups=[]
        self.caption_texts=[]
        for letter in ("A","B"):
            group=QGroupBox(f"Caption {letter}")
            text=QTextEdit()
            text.setReadOnly(True)
            text.setAccessibleName(f"Caption {letter} comparison text")
            group_layout=QVBoxLayout(group)
            group_layout.addWidget(text)
            caption_splitter.addWidget(group)
            self.caption_groups.append(group)
            self.caption_texts.append(text)
        caption_splitter.setSizes([1,1])
        captions_layout.addWidget(caption_splitter,1)
        main.addWidget(captions)
        main.setStretchFactor(0,4)
        main.setStretchFactor(1,6)
        main.setSizes([480,720])
        layout.addWidget(main,1)

        note=QLabel(
            "This comparison is temporary. Closing it discards the selected "
            "paths and generated captions; no preference, quality score, "
            "Image Knowledge, or model setting is saved."
        )
        note.setObjectName("mutedLabel")
        note.setWordWrap(True)
        layout.addWidget(note)

        navigation=QHBoxLayout()
        self.previous_button=QPushButton("Previous")
        self.previous_button.clicked.connect(lambda:self._move(-1))
        self.next_button=QPushButton("Next")
        self.next_button.clicked.connect(lambda:self._move(1))
        self.reveal_button=QPushButton(self._reveal_label)
        self.reveal_button.setProperty("primary",True)
        self.reveal_button.clicked.connect(self._reveal_current)
        navigation.addWidget(self.previous_button)
        navigation.addWidget(self.next_button)
        navigation.addStretch(1)
        navigation.addWidget(self.reveal_button)
        layout.addLayout(navigation)

        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(1260,760)
        self._refresh()

    def _move(self,offset: int) -> None:
        target=max(0,min(len(self.samples)-1,self.current_index+int(offset)))
        if target==self.current_index:
            return
        self.current_index=target
        self._refresh()

    def _reveal_current(self) -> None:
        self.revealed.add(self.current_index)
        self._refresh()

    def _refresh(self) -> None:
        sample=self.samples[self.current_index]
        order=self.mode_orders[self.current_index]
        captions=sample.get("captions",{})
        revealed=self.current_index in self.revealed
        self.sample_status.setText(
            f"Sample {self.current_index+1:,} of {len(self.samples):,}"
        )
        policy=self._content_safety_policy
        self.preview.policy=(
            policy
            if policy is not None
            and policy.cached_rating(self.paths[self.current_index]) is not None
            else None
        )
        self.preview.set_path(self.paths[self.current_index])
        for index,(letter,mode) in enumerate(zip(("A","B"),order)):
            title=f"Caption {letter}"
            if revealed:
                title+=f" — {self.variant_labels.get(mode,mode.upper())}"
            self.caption_groups[index].setTitle(title)
            self.caption_texts[index].setPlainText(str(captions.get(mode,"")))
            self.caption_texts[index].setAccessibleName(
                title+" comparison text"
            )
        self.previous_button.setEnabled(self.current_index>0)
        self.next_button.setEnabled(self.current_index<len(self.samples)-1)
        self.reveal_button.setEnabled(not revealed)
        self.reveal_button.setText(
            self._revealed_label if revealed else self._reveal_label
        )
        self._privacy_hidden=False

    def prepare_for_privacy_capture(self) -> None:
        self.preview.prepare_for_privacy_capture()
        for text in self.caption_texts:
            text.setPlainText("[Caption hidden for privacy capture]")
        self._privacy_hidden=True

    def changeEvent(self,event) -> None:
        super().changeEvent(event)
        if (
            event.type()==QEvent.ActivationChange
            and self.isActiveWindow()
            and self._privacy_hidden
        ):
            self._refresh()
