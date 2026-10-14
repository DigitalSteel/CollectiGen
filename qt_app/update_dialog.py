from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,QDialogButtonBox,QLabel,QTextBrowser,QVBoxLayout,
)


def public_update_channel_label(version: str,channel: str) -> str:
    """Translate the signed compatibility value into current public wording."""
    if channel=="stable":
        return "stable"
    prerelease=str(version).casefold().partition("-")[2]
    if prerelease.startswith("alpha"):
        return "prerelease (currently alpha)"
    if prerelease.startswith("beta"):
        return "prerelease (currently beta)"
    if prerelease.startswith("rc"):
        return "prerelease (currently release candidate)"
    return "prerelease"


class UpdateAvailableDialog(QDialog):
    """Show verified signed release notes before an update decision."""

    def __init__(self,version: str,channel: str,release_notes: str,parent=None):
        super().__init__(parent)
        self.setWindowTitle("Verified update available")
        self.setMinimumSize(700,520)
        layout=QVBoxLayout(self)
        public_channel=public_update_channel_label(version,channel)
        heading=QLabel(
            f"CollectiGen v{version} is available on the {public_channel} channel."
        )
        heading.setWordWrap(True)
        heading.setAccessibleName("Verified update version")
        layout.addWidget(heading)
        explanation=QLabel(
            "Review the signed release notes below. Downloading prepares the "
            "verified package; it is applied only after CollectiGen restarts."
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("mutedLabel")
        layout.addWidget(explanation)
        notes=QTextBrowser()
        notes.setMarkdown(str(release_notes or "No release notes were supplied."))
        notes.setOpenExternalLinks(False)
        notes.setAccessibleName(f"Release notes for CollectiGen v{version}")
        layout.addWidget(notes,1)
        buttons=QDialogButtonBox()
        prepare=buttons.addButton(
            "Download and Prepare Update",QDialogButtonBox.AcceptRole,
        )
        prepare.setProperty("primary",True)
        prepare.setDefault(True)
        later=buttons.addButton("Later",QDialogButtonBox.RejectRole)
        later.setAutoDefault(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def confirm_update_candidate(candidate,parent=None) -> bool:
    release=candidate.release
    return UpdateAvailableDialog(
        release.version,release.channel,release.release_notes,parent,
    ).exec()==QDialog.Accepted
