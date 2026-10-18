"""Separate source inventories and isolated Qt browser import closure."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def test_standalone_runtime_inventory_is_complete_and_separate(tmp_path):
    from tools.public_source import load_manifest
    inventory = json.loads((ROOT / "suite_core/encyclopedia/MODULE_MANIFEST.json").read_text())
    from suite_core.encyclopedia.version import SOFTWARE_VERSION, SNAPSHOT_INTERFACE_VERSION
    assert inventory["schema_version"] == 1
    assert inventory["software_version"] == SOFTWARE_VERSION
    assert inventory["snapshot_interface_version"] == SNAPSHOT_INTERFACE_VERSION
    runtime = inventory["runtime_files"]
    assert len(runtime) == len(set(runtime))
    assert "suite_core/knowledge/catalog_migration_preview.py" in runtime
    public = load_manifest(ROOT / "tools/public-source-manifest.json")
    assert set(runtime) | set(inventory["support_files"]) <= set(public["files"])
    assert "qt_app/browse_components.py" in public["files"]
    if public.get("export_scope") == "component-source":
        assert set(public["component_ids"]) & {"collectigen.tag-encyclopedia", "collectigen.suite-manager"}
        assert not public.get("shared_runtime_files")
        shared_runtime = ()  # Components declare their own finite runtime above.
    else:
        shared_runtime = public["shared_runtime_files"]
    assert len(shared_runtime) == len(set(shared_runtime))
    assert set(shared_runtime) <= set(public["files"])
    assert not any("encyclopedia" in name for name in shared_runtime)
    delivered = tmp_path / "module"
    for name in runtime:
        assert name.endswith(".py") or name == "suite_core/encyclopedia/RELEASE_PRESENTATION.json"
        assert not any(part in Path(name).parts for part in ("qualification", "tests", "__pycache__"))
        assert not name.endswith("local.json")
        target = delivered / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    assert {path.relative_to(delivered).as_posix() for path in delivered.rglob("*") if path.is_file()} == set(runtime)
    code = ("import sys;sys.path.insert(0,sys.argv[1]);"
            "import suite_core.encyclopedia.browser;import suite_core.encyclopedia.native_writer;"
            "assert {x for x in sys.modules if x=='core' or x.startswith('core.')}"
            " <= {'core','core.component_release_identity'};"
            "assert not any(x=='torch' or x.startswith('torch.') for x in sys.modules)")
    subprocess.run([sys.executable, "-I", "-B", "-c", code, str(delivered)], check=True, capture_output=True)


def test_focused_suite_selects_browser_and_distribution(tmp_path):
    from tools.test_suites import plan_for_paths, suite_test_modules
    # Exercise the stock selector with a controlled public-size tree. A full
    # maintainer checkout and a finite component export intentionally differ.
    included = {
        "suite_core/tests/test_encyclopedia.py",
        "suite_core/tests/test_encyclopedia_distribution.py",
        "tests/test_encyclopedia_compiled_routing.py",
    }
    for name in included | {"tests/test_unrelated_scope.py"}:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# inert selector fixture\n", encoding="utf-8")
    assert set(suite_test_modules("standalone-encyclopedia", tmp_path)) == included
    actual = set(suite_test_modules("standalone-encyclopedia", ROOT))
    assert included <= actual
    for path in ("suite_core/encyclopedia/browser.py", "suite_core/encyclopedia/MODULE_MANIFEST.json", "qt_app/browse_components.py"):
        assert "standalone-encyclopedia" in plan_for_paths((path,), ROOT)["suites"]


def test_source_entry_imports_and_opens_from_finite_inventory(tmp_path):
    """Offline source composition only: never open/import a catalog or update."""
    inventory = json.loads((ROOT / "suite_core/encyclopedia/MODULE_MANIFEST.json").read_text())
    runtime = inventory["runtime_files"]
    assert "core/encyclopedia_catalog.py" in runtime
    assert "suite_core/encyclopedia/catalog_ui.py" in runtime
    assert "qt_app/encyclopedia_help_commands.py" in runtime
    assert "core/encyclopedia_update_binding.py" in runtime
    assert all('suite_core/encyclopedia/'+name+'.py' in runtime
        for name in ('bootstrap_entry','entry_binding','control_entry','custody_entry'))
    delivered = tmp_path / "module"
    for name in set(runtime) | set(inventory["support_files"]):
        target = delivered / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    code = (
        "import os,sys;os.environ['QT_QPA_PLATFORM']='offscreen';"
        "sys.path.insert(0,sys.argv[1]);"
        "from pathlib import Path;from PySide6.QtWidgets import QApplication;"
        "from core.component_acquisition import ExactComponentOffer, ExactOfferAsset;"
        "from core.component_release_transport import ComponentReleaseTransport, ReleaseTransportPolicy;"
        "from core.encyclopedia_installation import EncyclopediaInstallation;"
        "from suite_core.discovery.github_component_releases import ReleaseLocator, AssetLocator;"
        "from suite_core.encyclopedia.hosted_examples import HostedLibraryBinding;"
        "from suite_core.encyclopedia.__main__ import create_window;"
        "from suite_core.encyclopedia.editing_ui import EncyclopediaEditingBridge;"
        "from qt_app.tag_details_host import TagDetailsEditorHost;"
        "from core.encyclopedia_catalog import prepare_selected_editor;"
        "from core.organizer_encyclopedia_admission import OrganizerCatalogAdmissionReader;"
        "from suite_core.media_organizer.encyclopedia_handoff_entry import OrganizerEncyclopediaHandoffReceiver;"
        "from suite_core.encyclopedia import __main__ as entry;"
        "from suite_core.encyclopedia import bootstrap_entry;assert bootstrap_entry.PROFILE is None;"
        "original_main=entry.main;entry.main=lambda **kwargs:kwargs;"
        "assert entry.run(['--theme','dark'])=={'argv':('--theme','dark')};"
        "entry.main=original_main;"
        "app=QApplication.instance() or QApplication([]);settings=Path(sys.argv[2]);"
        "w=create_window(settings_path=settings);"
        "from suite_core.encyclopedia.version import DISPLAY_VERSION;"
        "assert w.windowTitle()=='Tag Encyclopedia '+DISPLAY_VERSION;"
        "assert not settings.exists();assert w.close();app.processEvents();"
        "assert not settings.exists();"
        "assert not any(x=='torch' or x.startswith('torch.') for x in sys.modules)"
    )
    result=subprocess.run([sys.executable, "-I", "-B", "-c", code, str(delivered), str(tmp_path / "settings.json")],
                   capture_output=True)
    assert result.returncode==0,result.stderr.decode('utf-8',errors='replace')


def test_enrolled_updater_parser_imports_from_exact_finite_source_without_entry_activation(tmp_path):
    inventory = json.loads((ROOT / 'suite_core/encyclopedia/MODULE_MANIFEST.json').read_bytes())
    delivered = tmp_path / 'module'
    for name in set(inventory['runtime_files']) | set(inventory['support_files']):
        target = delivered / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    code = (
        "import sys;sys.path.insert(0,sys.argv[1]);"
        "import core.encyclopedia_update_binding as parser;"
        "import core.encyclopedia_enrollment;"
        "import json;from pathlib import Path;module=json.loads(Path(sys.argv[1],'suite_core/encyclopedia/MODULE_MANIFEST.json').read_bytes());assert parser._READ_CONTRACT_IDS=={p:frozenset(ids) for p,ids in module['update_data_contract']['read_contracts'].items()};parser._data_contract(json.dumps(module).encode(),module['software_version']);assert all(not ids for ids in parser._WRITE_CONTRACT_IDS.values());"
        "assert 'suite_core.encyclopedia.bootstrap_entry' not in sys.modules;"
        "assert not any(x.startswith(('PySide6','qt_app','torch')) for x in sys.modules)"
    )
    subprocess.run([sys.executable, '-I', '-B', '-c', code, str(delivered)],
                   check=True, capture_output=True)
