"""Bounded support for the original generated duplicate-custody test modules."""
from pathlib import Path
from time import monotonic
import os
import pytest
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from qt_test_support import QtTestScope

def wait_for(predicate):
    deadline=monotonic()+12
    while not predicate() and monotonic()<deadline:QTest.qWait(10)
    assert predicate()

def capture(widget,group,name):
    root=Path(os.environ['CG_DUPLICATE_CAPTURE_DIR']);out=root/group;out.mkdir(parents=True,exist_ok=True);path=out/(name+'.png');assert not path.exists();QApplication.processEvents();assert 0<widget.width()<=1920 and 0<widget.height()<=1080;assert widget.grab().save(str(path))

@pytest.fixture
def qt_test_scope():
    app=QApplication.instance() or QApplication([]);app.setQuitOnLastWindowClosed(False)
    font=QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf');assert font>=0;app.setFont(QFont(QFontDatabase.applicationFontFamilies(font)[0],10))
    with QtTestScope() as scope:yield scope
