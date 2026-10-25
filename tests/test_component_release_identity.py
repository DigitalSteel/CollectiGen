import pytest

from core.component_release_identity import ComponentReleaseIdentity, validate_legacy_display_mapping
from core.component_release_identity import display_component_version
from release_contract import ReleaseContractError, SemVer


@pytest.mark.parametrize("maturity,display", [("alpha", "a.0.3.0"), ("beta", "b.0.3.0"), ("stable", "0.3.0")])
def test_display_keeps_numeric_and_maturity_separate(maturity, display):
    identity = ComponentReleaseIdentity("0.3.0", maturity)
    assert identity.display_version == display
    assert identity.version == "0.3.0"


@pytest.mark.parametrize("version,maturity", [("a.0.3.0", "alpha"), ("0.3.0-alpha.1", "alpha"), ("0.3.0+build1", "alpha"), (" 0.3.0", "alpha"), ("00.3.0", "alpha"), ("0.3.0", "preview"), (None, "alpha")])
def test_rejects_wire_or_build_values_as_numeric_identity(version, maturity):
    with pytest.raises(ReleaseContractError):
        ComponentReleaseIdentity(version, maturity)


def test_numeric_order_precedes_maturity_and_promotions_are_structured():
    values = [ComponentReleaseIdentity("0.3.10", "alpha"), ComponentReleaseIdentity("0.3.9", "stable"), ComponentReleaseIdentity("0.3.9", "beta"), ComponentReleaseIdentity("0.3.9", "alpha")]
    assert [x.display_version for x in sorted(values, key=lambda x: x.sort_key)] == ["a.0.3.9", "b.0.3.9", "0.3.9", "a.0.3.10"]


def test_explicit_mapping_keeps_components_independent_and_legacy_parser_unchanged():
    rows = [("Manager", "0.2.1-alpha.9", ComponentReleaseIdentity("0.2.9", "alpha")), ("Manager", "0.2.1-alpha.10", ComponentReleaseIdentity("0.2.10", "alpha")), ("Organizer", "0.1.0-alpha.1", ComponentReleaseIdentity("0.1.1", "alpha"))]
    mapping = validate_legacy_display_mapping(reversed(rows))
    assert mapping["Manager", "0.2.1-alpha.10"].display_version == "a.0.2.10"
    assert ("Manager", "0.2.1-alpha.11") not in mapping
    assert SemVer.parse(rows[0][1]).compare(SemVer.parse(rows[1][1])) == -1


@pytest.mark.parametrize("legacy,target", [("0.2.1-alpha.10", "0.2.9"), ("0.2.1-alpha.10", "0.2.8"), ("0.2.1-alpha.9", "0.2.10"), ("0.2.1-alpha.9+other", "0.2.10")])
def test_mapping_rejects_collapse_downgrade_duplicate_or_build_renumbering(legacy, target):
    rows = [("Manager", "0.2.1-alpha.9", ComponentReleaseIdentity("0.2.9", "alpha")), ("Manager", legacy, ComponentReleaseIdentity(target, "alpha"))]
    with pytest.raises(ReleaseContractError):
        validate_legacy_display_mapping(rows)


def test_mapping_cannot_relabel_alpha_as_stable_or_guess_unknown_maturity():
    for legacy, maturity in [("0.2.1-alpha.1", "stable"), ("0.2.1-rc.1", "alpha")]:
        with pytest.raises(ReleaseContractError):
            validate_legacy_display_mapping([("Manager", legacy, ComponentReleaseIdentity("0.2.1", maturity))])


def test_known_labels_include_legacy_on_request_without_guessing_unknown_versions():
    assert display_component_version("suite-manager", "0.2.1-alpha.15") == "a.0.2.15"
    assert display_component_version("suite-manager", "0.2.1-alpha.16") == "a.0.2.16"
    assert display_component_version("suite-manager", "0.3.0-alpha.1") == "a.0.3.0"
    assert display_component_version("suite-manager", "0.3.1-alpha.1") == "a.0.3.1"
    assert display_component_version("suite-manager", "0.3.2-alpha.1") == "a.0.3.2"
    assert display_component_version("collectigen.tag-encyclopedia", "0.1.0-alpha.8", include_legacy=True) == "a.0.1.8 (embedded 0.1.0-alpha.8)"
    assert display_component_version("ollama", "0.5.0") == "0.5.0"
    assert display_component_version("suite-manager", "0.2.1-alpha.99") == "0.2.1-alpha.99"
