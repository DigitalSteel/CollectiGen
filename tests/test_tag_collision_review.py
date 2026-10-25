"""Shared original collision dialog choices; synthetic records, no stores."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt,QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox,QWidget

from qt_app.knowledge_page import KnowledgePage


@pytest.mark.parametrize('movable,alias,record_present,owners,expect_move',[
    (False,'other spelling',True,('owner',),False),
    (True,'other spelling',True,('owner',),True),
    (True,'owner name',True,('owner',),False),
    (True,'other spelling',False,('owner',),False),
    (True,'other spelling',True,('owner','second'),False),
])
def test_shared_collision_move_is_explicit_noncanonical_single_owner(
        qt_test_scope,movable,alias,record_present,owners,expect_move):
    widget=qt_test_scope.show(QWidget())
    owner=SimpleNamespace(id='owner',display='owner name')
    second=SimpleNamespace(id='second',display='second owner')
    widget.tags=SimpleNamespace(tags={'owner':owner,'second':second})
    widget.categories=SimpleNamespace(path_label=lambda _: 'Synthetic')
    owner.category=second.category='synthetic'
    widget._tag_aliases=lambda _: ['other spelling']
    widget.tag_name=SimpleNamespace(text=lambda:'new synthetic tag')
    record=SimpleNamespace(id='target',display='target name',category='synthetic') if record_present else None
    errors=[];observed=[]
    timer=qt_test_scope.own_timer(QTimer());timer.setInterval(10)
    def choose():
        box=widget.findChild(QMessageBox)
        if box is None or not box.isVisible():return
        try:
            labels=[button.text() for button in box.buttons()]
            assert ('Move Other Name' in labels)==expect_move
            assert box.defaultButton()==box.button(QMessageBox.Cancel)
            observed.append(labels)
            button=next(button for button in box.buttons() if button.text()=='Move Other Name') if expect_move else box.button(QMessageBox.Cancel)
            QTest.mouseClick(button,Qt.LeftButton)
        except Exception as error:
            errors.append(error);box.reject()
    timer.timeout.connect(choose);timer.start()
    result=KnowledgePage._review_alias_collision(widget,alias,record,owners,movable=movable)
    timer.stop()
    assert observed and not errors,errors
    assert result==('move' if expect_move else 'cancel')
