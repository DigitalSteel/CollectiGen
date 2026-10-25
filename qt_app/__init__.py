"""Qt presentation layer for CollectiGen.

The window is imported lazily so migration audits and other source-level tools
do not require PySide6 merely to inspect the Qt package.
"""

__all__=["TagManagerWindow"]


def __getattr__(name):
    if name == "TagManagerWindow":
        from .main_window import TagManagerWindow

        return TagManagerWindow
    raise AttributeError(name)
