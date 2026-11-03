"""Generated indicator regression using the selected application's Qt runtime.

Run with its bundled Python -I -B and an explicit fresh --output directory.
No application profile, library, network, report or retained fixture is opened.
"""
import argparse,json
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap,QFont,QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QCheckBox,QRadioButton,QTreeWidget,QTreeWidgetItem
from qt_app.theme import apply_theme,build_stylesheet

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    assert not args.output.exists();args.output.mkdir(parents=True)
    app=QApplication([]);app.setQuitOnLastWindowClosed(False);results=[]
    # Match the existing source harness's explicit Windows offscreen font.
    font_id=QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf');assert font_id>=0
    app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0],10))
    def raster(widget):
        image=widget.grab().toImage();return bytes(image.constBits())
    for theme in ('light','dark','midnight'):
        apply_theme(app,theme,theme=='midnight');root=QWidget();root.setWindowTitle('Generated selection-state review');layout=QVBoxLayout(root)
        layout.addWidget(QLabel('Generated selection states — no user profile or image data'))
        checks=[]
        for label,state,enabled in [('Unchecked',Qt.Unchecked,True),('Checked',Qt.Checked,True),('Partially checked',Qt.PartiallyChecked,True),('Disabled unchecked',Qt.Unchecked,False),('Disabled checked',Qt.Checked,False),('Disabled partial',Qt.PartiallyChecked,False)]:
            row=QHBoxLayout();row.addWidget(QLabel(label));widget=QCheckBox('Include generated item');widget.setTristate(True);widget.setCheckState(state);widget.setEnabled(enabled);row.addWidget(widget);layout.addLayout(row);checks.append(widget)
        for label,checked,enabled in [('Radio unselected',False,True),('Radio selected',True,True),('Radio disabled selected',True,False)]:
            row=QHBoxLayout();row.addWidget(QLabel(label));widget=QRadioButton('Generated option');widget.setAutoExclusive(False);widget.setChecked(checked);widget.setEnabled(enabled);row.addWidget(widget);layout.addLayout(row)
        tree=QTreeWidget();tree.setHeaderLabels(['Generated item-view selection'])
        for label,state in [('Included',Qt.Checked),('Excluded',Qt.Unchecked),('Mixed',Qt.PartiallyChecked)]:
            item=QTreeWidgetItem([label]);item.setFlags(item.flags()|Qt.ItemIsUserCheckable);item.setCheckState(0,state);tree.addTopLevelItem(item)
        layout.addWidget(tree);root.resize(1050,850);root.show();root.activateWindow();app.processEvents()
        for tone in ('light','dark'):
            for glyph in ('check','partial','radio'):assert not QPixmap(':/collectigen/indicators/'+glyph+'-'+tone+'.svg').isNull()
        assert len({raster(w) for w in checks[:3]})==3
        assert raster(checks[0])!=raster(checks[3]) and raster(checks[1])!=raster(checks[4])
        before=raster(checks[1]);checks[1].setFocus();app.processEvents();assert checks[1].hasFocus() and raster(checks[1])!=before
        assert root.grab().save(str(args.output/(theme+'.png')))
        checks[0].setTristate(False);checks[0].setFocus();QTest.keyClick(checks[0],Qt.Key_Space);assert checks[0].isChecked()
        tree.setCurrentItem(tree.topLevelItem(1));tree.setFocus();QTest.keyClick(tree,Qt.Key_Space);assert tree.topLevelItem(1).checkState(0)==Qt.Checked
        results.append(dict(theme=theme,marks_focus_disabled_keyboard=True,all_six_resources=True));root.close();root.deleteLater();app.processEvents()
    assert '/collectigen/indicators/' not in build_stylesheet('system')
    receipt=dict(passed=True,themes=results,system_native_style_preserved=True,profile_access=False,private_data_access=False)
    (args.output/'RESULT.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8');print(json.dumps(receipt));app.quit()

if __name__=='__main__':main()
