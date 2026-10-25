"""Ribbon registrations for secondary CollectiGen workspaces.

Only commands whose scope is the whole workspace live here.  Search fields,
selection actions, record editors, and save/cancel boundaries deliberately stay
next to the content they affect.
"""
from __future__ import annotations

from PySide6.QtWidgets import QMenu, QToolButton

from .command_bar import UiCommand, bind_source


def _source(page, attribute):
    source = getattr(page, attribute, None)
    return source if source is not None else None


def _bound(page, key, attribute, group, *, label=None, keywords=(), description="", context=""):
    source = _source(page, attribute)
    if source is None:
        return None
    return bind_source(
        page, key, source, group, move=True, label=label, keywords=keywords,
        description=description, context=context,
    )


def _review_commands(page):
    context = "Review Center"
    command = _bound(
        page, "review.recheck", "recheck_button", "Review",
        keywords=("rebuild", "questions", "queue"),
        description="Rebuild Review questions from saved Image Knowledge and reviewed rules.",
        context=context,
    )
    commands=[command] if command is not None else []
    reload=_bound(page,"review.reload","reload_queue_button","Review",
        keywords=("reload","saved","queue"),context=context)
    if reload is not None:commands.append(reload)
    return commands


def _knowledge_commands(page):
    original=getattr(page,"knowledge_more_button",None)
    if original is None:return []
    # Keep the dynamic original menu authoritative. Build each filtered view
    # only when opened; never cache a QAction destroyed by the next rebuild.
    specifications=(
        ("Discover Knowledge…","Knowledge",("Find New Knowledge…",)),
        ("Relationships…","Knowledge",("Manage Relationships…","Review Character Tag Links…")),
        ("Maintenance…","Knowledge",("Restore Shipped Values…","Propose Local Changes as Future Defaults…","Review Removed Vocabulary…","Review Safe Fixes…")),
        ("Tag Colors…","View",("Manage Tag Colors…",)),
        ("Saved Views…","View",("Save Current View…","Delete Current Saved View…")),
    )
    commands=[]
    for index,(label,group,labels) in enumerate(specifications):
        source=QToolButton(page);source.setText(label)
        menu=QMenu(source);source.setMenu(menu)
        def populate(target=menu,allowed=labels):
            target.clear()
            original.menu().aboutToShow.emit()
            for action in original.menu().actions():
                if action.text() in allowed:target.addAction(action)
        menu.aboutToShow.connect(populate)
        source.setProperty("commandSearchMenuOnly",True)
        source.show()
        commands.append(bind_source(page,f"knowledge.group.{index}",source,group,
            move=True,context="Tag Encyclopedia",keywords=labels))
    original.setParent(page._command_source_holder);original.hide()
    return commands


def _automation_commands(page):
    command = _bound(
        page, "automation.resolution_policy", "resolution_policy_button", "Settings",
        label="Resolution Policy…",
        keywords=("resolution", "resize", "dimensions", "policy"),
        context="Rules & Automation",
    )
    commands=[command] if command is not None else []
    for attribute,group,label,keywords in (
        ("discover_templates_button","Templates","Discover Tag Families…",("discover","reusable","templates")),
        ("build_template_button","Templates","Build Reusable Tags…",("create","template","builder")),
        ("import_knowledge_pack_button","Knowledge Packs",None,("import","knowledge","pack")),
        ("export_knowledge_pack_button","Knowledge Packs",None,("export","knowledge","pack")),
    ):
        command=_bound(page,"automation."+attribute,attribute,group,
            label=label,keywords=keywords,context="Rules & Automation")
        if command is not None:commands.append(command)
    pack_button=getattr(page,"import_knowledge_pack_button",None)
    if pack_button is not None:
        packs=getattr(pack_button,"_command_original_parent",None)
        tabs=getattr(page,"value_sections",None)
        if tabs is not None and packs is not None:
            index=tabs.indexOf(packs)
            if index>=0:tabs.setTabVisible(index,False)
    return commands


def _setup_commands(page):
    command=_bound(page,"setup.logs","open_logs_button","Help",
        keywords=("logs","diagnostics"),context="Setup")
    return [command] if command is not None else []


def secondary_commands(window, page, index: int) -> list[UiCommand]:
    """Return static page-wide commands for navigation indexes 0, 3–6.

    ``window`` is accepted for the shared registration interface.  These
    registrations route to the original page controls, so their callbacks,
    menus, enabled state, and confirmation/recovery behavior remain authority.
    """
    del window
    if index == 3:
        return _review_commands(page)
    if index == 4:
        return _knowledge_commands(page)
    if index == 5:
        return _automation_commands(page)
    if index == 6:
        return _setup_commands(page)
    # Dashboard goal cards retain their workflow context.
    return []
