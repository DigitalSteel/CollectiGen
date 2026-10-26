"""Shared reviewed Knowledge Pack UI actions; persistence stays in core."""
from pathlib import Path
from PySide6.QtWidgets import QFileDialog
from .status_notifications import FooterMessageBox as QMessageBox
from .knowledge_pack_dialog import KnowledgePackExportDialog, KnowledgePackImportDialog
from core import (
    plan_knowledge_pack_export, plan_knowledge_pack_import,
    export_knowledge_pack, import_knowledge_pack,
)


def export_pack(parent):
    root=Path(__file__).resolve().parents[1]
    try: plan=plan_knowledge_pack_export(root)
    except Exception as exc: QMessageBox.critical(parent,"Could not inspect custom knowledge",str(exc)); return
    if not plan.can_export:
        QMessageBox.information(parent,"Nothing to export","There is no custom knowledge to place in a Knowledge Pack. Built-in knowledge is already included with CollectiGen."); return
    dialog=KnowledgePackExportDialog(plan,parent)
    if not dialog.exec():return
    target=QFileDialog.getSaveFileName(parent,"Export Knowledge Pack","CollectiGen_KnowledgePack.zip","ZIP archive (*.zip)")[0]
    if not target: return
    try: output=export_knowledge_pack(root,Path(target),plan=plan)
    except Exception as exc: QMessageBox.critical(parent,"Export failed",str(exc)); return
    QMessageBox.information(parent,"Export complete",f"Knowledge Pack created:\n\n{output}",
        summary=f"Knowledge Pack exported: {output.name}")

def import_pack(parent):
    source=QFileDialog.getOpenFileName(parent,"Import Knowledge Pack","","ZIP archive (*.zip)")[0]
    if not source: return
    root=Path(__file__).resolve().parents[1]
    try: plan=plan_knowledge_pack_import(root,Path(source))
    except Exception as exc: QMessageBox.critical(parent,"Could not inspect Knowledge Pack",str(exc)); return
    dialog=KnowledgePackImportDialog(plan,parent)
    if not dialog.exec(): return
    try: results=import_knowledge_pack(root,Path(source),plan=plan)
    except Exception as exc:
        QMessageBox.critical(parent,"Import not confirmed",
            "The import stopped before completion was confirmed.\n\n"
            f"Details: {exc}\n\n"
            "If recovery needs review, keep its backups and receipt. Do not delete or replace them. "
            "Retain this diagnostic for assistance; restarting alone may not resolve it.")
        return
    added=sum(int(value.get("added",0)) for value in results.values() if isinstance(value,dict))
    updated=sum(int(value.get("overridden",0))+int(value.get("disabled_defaults",0)) for value in results.values() if isinstance(value,dict))
    conflicts=sum(int(value.get("conflicts",0)) for value in results.values() if isinstance(value,dict))
    QMessageBox.information(parent,"Import complete",f"Imported {added:,} new knowledge record(s).\nUpdated reviewed defaults: {updated:,}.\nNon-relationship alternatives kept for review: {conflicts:,}.\n\nRestart CollectiGen to reload every registry.",
        summary=f"Import complete: {added:,} new records, {updated:,} updates. Restart CollectiGen to reload Knowledge.")
