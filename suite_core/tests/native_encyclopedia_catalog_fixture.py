"""Synthetic standalone shell capture; no native database, image or network read."""
from pathlib import Path
import argparse
import json
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    assert not args.output.exists()
    args.output.mkdir(parents=True)
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont, QFontDatabase
    from qt_app.theme import apply_theme
    from suite_core.encyclopedia.__main__ import create_window
    from suite_core.tests.test_encyclopedia import fixture
    app = QApplication.instance() or QApplication([])
    font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
    assert font_id >= 0, 'Fixture font must be explicitly available for readable captures'
    family = QFontDatabase.applicationFontFamilies(font_id)[0]
    receipts = []
    for theme in ('light', 'dark'):
        apply_theme(app, theme, False)
        app.setFont(QFont(family, 10))
        window = create_window(settings_path=args.output / (theme + '-settings.json'))
        window.resize(1920, 1080); window.show(); app.processEvents()
        filename = args.output / (theme + '-empty.png')
        assert window.grab().save(str(filename))
        receipts.append({'theme': theme, 'state': 'empty', 'capture': str(filename)})
        window.open_publication(fixture(24))
        window.table.setCurrentIndex(window.model.index(0, 0))
        app.processEvents()
        filename = args.output / (theme + '-browse.png')
        assert window.grab().save(str(filename))
        receipts.append({'theme': theme, 'state': 'synthetic-browse', 'capture': str(filename)})
        window.bar.select_group('Help'); app.processEvents()
        filename = args.output / (theme + '-help.png')
        assert window.grab().save(str(filename))
        receipts.append({'theme': theme, 'state': 'offline-help', 'capture': str(filename)})
        window.close(); app.processEvents()
    assert not any(args.output.glob('*settings.json'))
    (args.output / 'CAPTURES.json').write_text(json.dumps(receipts, indent=2) + '\n')


if __name__ == '__main__':
    main()
