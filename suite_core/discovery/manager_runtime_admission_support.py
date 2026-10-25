"""Inactive admission support extracted verbatim from accepted r2. No host binding."""
from dataclasses import dataclass
from xml.parsers import expat
import xml.etree.ElementTree as ET
from suite_core.installation import ComponentId
from .component_staging import require, MAX_METADATA_BYTES

MANAGER = ComponentId("collectigen.suite-manager")

PREFIX = "lib/app/"

NS = "{http://schemas.microsoft.com/packaging/2010/07/nuspec.xsd}"

@dataclass(frozen=True)
class AdmissionPlan:
    component_id: str
    version: str
    channel: str
    artifact_sha256: str
    release_sha256: str
    trust_sha256: str
    inventory_sha256: str
    payload_mapping: tuple
    remaining_gates: tuple = (
        "producer/stock-SDK mapping qualification", "hook-first frozen launcher",
        "external settings and diagnostics", "known-good recovery launcher",
        "owned lock/positive-drain handoff", "real restart/health/recovery",
    )

    @property
    def production_install_authorized(self):
        return False


def _metadata(raw):
    """Bounded UTF-8 only, optional UTF-8 BOM; no DTD/entity processing.

    No encoding declaration or a case-insensitive UTF-8 declaration is allowed.
    UTF-16/32, legacy encodings, NULs and alternate encoding aliases are refused.
    Expat's declaration callbacks enforce the policy before ElementTree sees the
    document; byte-pattern scanning is not an XML security boundary. Signed raw
    archive/inventory bytes are unchanged by this metadata-only decoding.
    """
    require(0 < len(raw) <= MAX_METADATA_BYTES, "nuspec_size")
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("xml_declarations_refused") from exc
    require("\0" not in text, "xml_declarations_refused")
    def refuse(*args):
        raise ValueError("xml_declarations_refused")
    def declaration(version, encoding, standalone):
        require(encoding is None or encoding.casefold() == "utf-8",
                "xml_declarations_refused")
    parser = expat.ParserCreate(encoding="UTF-8")
    parser.XmlDeclHandler = declaration
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(text.encode("utf-8"), True)
        root = ET.fromstring(text)
    except (expat.ExpatError, ET.ParseError) as exc:
        raise ValueError("nuspec_syntax") from exc
    require(root.tag == NS + "package" and len(root) == 1, "nuspec_package")
    metadata = root[0]
    require(metadata.tag == NS + "metadata", "nuspec_metadata")
    fields = {}
    for child in metadata:
        require(child.tag.startswith(NS) and len(child) == 0 and not child.attrib,
                "nuspec_field")
        key = child.tag[len(NS):]
        require(key not in fields, "nuspec_duplicate")
        fields[key] = child.text
    return fields
