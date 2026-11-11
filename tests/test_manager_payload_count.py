"""Exercise the measured Manager payload count and its finite archive boundary."""
import json
import zipfile
import pytest
from suite_core.discovery.manager_runtime_detached_admission import (
    MANAGER, PREFIX, PROFILE, PAYLOAD_ENTRY, PACKAGE_STUB, produce_detached_inventory,
)


@pytest.mark.parametrize("count", [1286, 1536, 1537])
def test_manager_payload_count_boundary(tmp_path, count):
    version = "0.6.0-alpha.1"
    values = dict(id=MANAGER.value, version=version, channel="beta-win-x64",
                  mainExe=PAYLOAD_ENTRY, os="win", rid="win-x64", machineArchitecture="x64")
    xml = ('<package xmlns="http://schemas.microsoft.com/packaging/2010/07/nuspec.xsd"><metadata>' + "".join(f"<{k}>{v}</{k}>" for k, v in values.items())
           + "</metadata></package>").encode()
    path = tmp_path / (MANAGER.value + "-" + version + "-beta-win-x64-full.nupkg")
    members = {MANAGER.value + ".nuspec": xml, "[Content_Types].xml": b"<Types/>",
               "_rels/.rels": b"<Relationships/>", PREFIX + "sq.version": xml,
               PREFIX + PAYLOAD_ENTRY: b"INERT PAYLOAD", PREFIX + PACKAGE_STUB: b"INERT STUB",
               PREFIX + "Squirrel.exe": b"INERT UPDATER"}
    members.update({PREFIX + f"resources/file-{i:04d}.bin": b"x" for i in range(count - 4)})
    with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as z:
        for name, content in members.items():
            z.writestr(name, content)
    if count > 1536:
        with pytest.raises(ValueError, match="archive_member_count"):
            produce_detached_inventory(path, inventory_profile=PROFILE,
                                      expected_version=version, expected_channel="beta")
    else:
        raw = produce_detached_inventory(path, inventory_profile=PROFILE,
                                         expected_version=version, expected_channel="beta")
        assert len(json.loads(raw)["files"]) == count
