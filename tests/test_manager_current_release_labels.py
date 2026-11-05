"""Synthetic source-only release labels; no launch, acquisition or installed authority."""
from datetime import datetime, timezone
from dataclasses import replace
import os
from pathlib import Path
import pytest
from core.component_release_identity import display_component_version

@pytest.mark.parametrize("aliases,wire,label", [
 (("TagEncyclopedia","tag-encyclopedia","collectigen.tag-encyclopedia"),"0.3.1-alpha.1","a.0.3.1"),
 (("MediaOrganizer","media-organizer","collectigen.media-organizer"),"0.4.23-alpha.1","a.0.4.23"),
 (("SuiteManager","suite-manager","collectigen.suite-manager"),"0.5.1-alpha.1","a.0.5.1")])
def test_current_labels_preserve_wire_and_unknown_fallback(aliases,wire,label):
 for alias in aliases:
  assert display_component_version(alias,wire)==label
  assert display_component_version(alias,wire,include_legacy=True)==f"{label} (embedded {wire})"
  assert display_component_version(alias,"99.0.0-alpha.99")=="99.0.0-alpha.99"

@pytest.mark.parametrize("theme", ["light","dark"])
def test_actual_manager_cards_and_inventory_labels(theme,tmp_path):
 from PySide6.QtWidgets import QApplication,QLabel,QTextBrowser,QToolButton,QPushButton
 from suite_core.manager.window import create_window,unconfigured,DevelopmentComponent,SupportedAppPreview
 from suite_core.manager.ui_cards import development_card
 from suite_core.installation import ComponentId
 from suite_core.manager.candidate_inventory import CandidatePackageObservation
 app=QApplication.instance() or QApplication([])
 from qt_app.theme import apply_theme
 from PySide6.QtGui import QFontDatabase,QFont
 font_path=os.environ.get("MANAGER_LABEL_FONT")
 if font_path:
  font_id=QFontDatabase.addApplicationFont(font_path);assert font_id>=0
  families=QFontDatabase.applicationFontFamilies(font_id);assert families
  app.setFont(QFont(families[0],10))
 apply_theme(app,theme)
 now=datetime(2026,11,5,tzinfo=timezone.utc)
 rows=(("tag-encyclopedia","Tag Encyclopedia","0.3.1-alpha.1","a.0.3.1"),
       ("media-organizer","Media Organizer","0.4.23-alpha.1","a.0.4.23"))
 entries=tuple(SupportedAppPreview(ComponentId(key),title,"synthetic",now,
  description="Synthetic source label evidence",record_kind="developer candidate",candidate_package=CandidatePackageObservation(key,wire,"synthetic-not-read.zip","0"*64,"1"*64,now,0,0,1,(1,),(),"Synthetic; not executable",()))
  for key,title,wire,label in rows)
 view=replace(unconfigured(now),synthetic=True,supported_apps=entries)
 window=create_window(view,theme=theme,reader_settings_path=tmp_path/'unused-settings.json')
 window.resize(1920,1080)
 try:
  window.show();window.navigation.setCurrentRow(window.page_rows['Applications']);app.processEvents()
  capture=os.environ.get("MANAGER_LABEL_CAPTURE_DIR")
  if capture:
   target=Path(capture);target.mkdir(parents=True,exist_ok=True)
   assert window.grab().save(str(target/f"inventory-{theme}.png"))
  labels=[x.text() for x in window.findChildren(QLabel)]
  for key,title,wire,label in rows:
   assert f"{label} \u00b7 Unsigned developer candidate" in labels
   card=development_card(DevelopmentComponent(key,title,"Synthetic source preview",(),"Not installable",wire,"2026-11-05"))
   try:
    card.resize(620,600);card.show()
    toggle=card.findChild(QToolButton);toggle.click();app.processEvents()
    text=card.findChild(QTextBrowser).toPlainText()
    assert f"Source version: {label} (embedded {wire})" in text
    assert not card.findChild(QPushButton).isEnabled()
    if capture:assert card.grab().save(str(target/f"card-{key}-{theme}.png"))
   finally:card.close();card.deleteLater()
  assert window.size().width()==1920 and window.size().height()==1080
 finally:
  window.close();app.processEvents();window.deleteLater()
