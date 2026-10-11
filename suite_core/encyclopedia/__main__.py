"""Developer entry point: python -B -m suite_core.encyclopedia [--snapshot FILE]."""
import argparse


def main():
    parser = argparse.ArgumentParser(description="Read-only Tag Encyclopedia")
    parser.add_argument("--snapshot", help="Explicit approved published snapshot")
    arguments = parser.parse_args()
    from PySide6.QtWidgets import QApplication
    from .browser import EncyclopediaWindow
    app = QApplication([])
    window = EncyclopediaWindow()
    if arguments.snapshot:
        window.open_snapshot(arguments.snapshot)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
