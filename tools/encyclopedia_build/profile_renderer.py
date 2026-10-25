"""Build-only reviewed public profile renderers; never activate source PROFILE."""
from pathlib import Path
import ast
from hashlib import sha256
import json
import re

from tools.build_manager_distribution import BuildHold, require, safe_path
from suite_core.discovery.component_release_signature import _json, MAX_METADATA_BYTES

PROFILE_FIELDS = {'schema', 'purpose', 'fixture_root', 'data_relative', 'settings_relative',
    'cache_relative', 'trust_hex', 'trust_sha256', 'responses_sha256', 'channel', 'host_version'}
PURPOSE = 'ENCYCLOPEDIA_ISOLATED_QUALIFICATION_ONLY'
NORMAL_PURPOSE = 'ENCYCLOPEDIA_NORMAL_PORTABLE_PROFILE'
NORMAL_FIELDS = {'schema', 'purpose', 'layout_id', 'trust_hex', 'trust_sha256',
    'channel', 'host_version', 'transport_id', 'asset_redirect_origins'}


def validate_normal_profile(value):
    require(type(value) is dict and set(value) == NORMAL_FIELDS
        and type(value['schema']) is int and value['schema'] == 1
        and value['purpose'] == NORMAL_PURPOSE, 'exact_normal_profile_required')
    require(value['layout_id'] == 'portable-executable-relative-v1'
        and value['transport_id'] == 'github-release-assets-https'
        and value['asset_redirect_origins'] == ['https://release-assets.githubusercontent.com'],
        'fixed_normal_layout_transport_required')
    require(value['channel'] in ('stable', 'beta') and type(value['host_version']) is str,
        'explicit_normal_channel_version_required')
    from suite_core.discovery.component_release_signature import _version, _trust
    _version(value['host_version'])
    require(type(value['trust_hex']) is str and 0 < len(value['trust_hex']) <= 2 * MAX_METADATA_BYTES,
        'public_trust_bounds')
    try:
        trust = bytes.fromhex(value['trust_hex'])
    except ValueError as error:
        raise BuildHold('public_trust_hex_required') from error
    require(type(value['trust_sha256']) is str and sha256(trust).hexdigest() == value['trust_sha256'],
        'public_trust_pin_changed')
    document = _trust(trust)
    require(document['keys'] and all(set(key) == {'key_id', 'algorithm', 'modulus', 'exponent', 'status'}
        for key in document['keys']) and any(key['status'] == 'active' for key in document['keys']),
        'explicit_active_public_trust_required')
    return dict(value, asset_redirect_origins=list(value['asset_redirect_origins']))


def validate_profile(value):
    require(type(value) is dict and set(value) == PROFILE_FIELDS and type(value['schema']) is int
        and value['schema'] == 1 and value['purpose'] == PURPOSE, 'exact_fixture_profile_required')
    root = Path(value['fixture_root'])
    require(type(value['fixture_root']) is str and root.is_absolute()
        and root == root.absolute(), 'absolute_disposable_fixture_root_required')
    data = safe_path(value['data_relative'])
    settings = safe_path(value['settings_relative'])
    cache = safe_path(value['cache_relative'])
    require(data == 'Data' and settings.startswith('Data/') and settings != data
        and cache.startswith('Data/Encyclopedia/UpdateCache/') and cache != settings,
        'exact_fixture_data_cache_scope_required')
    require(value['channel'] in {'stable', 'beta'} and type(value['host_version']) is str,
        'fixed_fixture_channel_version_required')
    from suite_core.discovery.component_release_signature import _version
    _version(value['host_version'])
    require(type(value['trust_hex']) is str and len(value['trust_hex']) <= 2 * MAX_METADATA_BYTES,
        'public_trust_bounds')
    try:
        trust = bytes.fromhex(value['trust_hex'])
    except ValueError as error:
        raise BuildHold('public_trust_hex_required') from error
    require(type(value['trust_sha256']) is str and sha256(trust).hexdigest() == value['trust_sha256'],
        'public_trust_pin_changed')
    document = _json(trust, 'fixture public trust')
    require(set(document) == {'schema_version', 'keys'} and type(document['schema_version']) is int
        and document['schema_version'] == 1 and type(document['keys']) is list and 1 <= len(document['keys']) <= 16,
        'explicit_public_fixture_trust_required')
    for key in document['keys']:
        require(type(key) is dict and set(key) == {'key_id', 'algorithm', 'modulus', 'exponent', 'status'},
            'public_only_fixture_key_fields_required')
    require(type(value['responses_sha256']) is str
        and re.fullmatch('[0-9a-f]{64}', value['responses_sha256']), 'sealed_responses_pin_required')
    return dict(value)


RUNTIME = r'''# Generated qualification-only public inputs; NOT production activation.
from pathlib import Path
from hashlib import sha256
import io
from uuid import uuid4
from urllib.parse import urlsplit
from core.component_acquisition import ComponentAcquisition
from release_contract import sha256_file
from suite_core.discovery.component_staging import guard, MAX_ARCHIVE
from core.manager_stock_apply import readback
from suite_core.discovery.component_release_signature import _json, MAX_METADATA_BYTES
from suite_core.discovery.github_component_releases import REPOSITORY
from suite_core.encyclopedia.entry_binding import StandaloneEntryProfile

class SealedEncyclopediaFixtureOpener:
    def __call__(self, request, **options):
        descriptor = ROOT / '_bootstrap/qualification-responses.json'
        guard(descriptor)
        if descriptor.stat().st_size > MAX_METADATA_BYTES:
            raise ValueError('sealed_fixture_mapping_bounds')
        raw = descriptor.read_bytes()
        if sha256(raw).hexdigest() != INPUT['responses_sha256']:
            raise ValueError('sealed_fixture_mapping_changed')
        document = _json(raw, 'fixture responses')
        if set(document) != {'schema', 'responses'} or type(document['schema']) is not int or document['schema'] != 1:
            raise ValueError('closed_fixture_response_map_required')
        responses = document['responses']
        if type(responses) is not dict or not 1 <= len(responses) <= 32:
            raise ValueError('closed_fixture_response_map_required')
        url = request.full_url
        listing = {f'https://api.github.com/repos/{REPOSITORY}/releases?per_page=50&page={p}' for p in (1, 2, 3)}
        parts = urlsplit(url)
        asset = (parts.scheme == 'https' and parts.netloc == 'github.com' and not parts.query and not parts.fragment
            and parts.path.startswith('/' + REPOSITORY + '/releases/download/collectigen.tag-encyclopedia%2Fv')
            and len(parts.path.split('/')) == 7)
        if request.get_method() != 'GET' or url not in responses or not (url in listing or asset):
            raise ValueError('unknown_encyclopedia_fixture_url')
        spec = responses[url]
        if (type(spec) is not dict or set(spec) != {'path', 'size', 'sha256'} or type(spec['size']) is not int
                or not 0 < spec['size'] <= 512 * 1024 * 1024 or type(spec['path']) is not str
                or type(spec['sha256']) is not str or len(spec['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in spec['sha256'])):
            raise ValueError('exact_fixture_response_required')
        relative = Path(spec['path'])
        if (relative.is_absolute() or not relative.parts or relative.parts[0] != '_qualification'
                or '..' in relative.parts or ':' in spec['path'] or chr(92) in spec['path']):
            raise ValueError('sealed_fixture_response_scope_required')
        path = ROOT / relative
        guard(path)
        readback(path, spec['size'], spec['sha256'])
        header = request.get_header('Range')
        offset = 0
        if header is not None:
            if not header.startswith('bytes=') or not header.endswith('-') or not header[6:-1].isascii() or not header[6:-1].isdigit():
                raise ValueError('fixture_range_required')
            offset = int(header[6:-1])
            if offset >= spec['size']:
                raise ValueError('fixture_range_bound')
        class Response(io.BufferedReader):
            status = 206 if header is not None else 200
            def getcode(self): return self.status
        result = Response(path.open('rb'))
        result.seek(offset)
        return result

ROOT = Path(INPUT['fixture_root'])
TRUST = bytes.fromhex(INPUT['trust_hex'])

def make_profile(role):
    import sys
    if role not in ('root', 'reader', 'custody') or not getattr(sys, 'frozen', False):
        raise ValueError('fixed_frozen_fixture_role_required')
    actual = Path(sys.executable).absolute()
    if (role in ('root', 'custody') and actual != ROOT / 'TagEncyclopedia.exe'
            or role == 'reader' and not actual.is_relative_to(ROOT / 'Apps/collectigen.tag-encyclopedia')):
        raise ValueError('actual_fixture_role_layout_required')
    # Observed installed-image identity, not publisher auth; no self-hash cycle.
    bootstrap_pin = sha256_file(ROOT / 'TagEncyclopedia.exe')
    acquisition = None
    if role == 'root':
        # Retain previous acquisitions; each new service owns a fresh empty leaf.
        cache_parent = ROOT / INPUT['cache_relative']
        guard(cache_parent)
        if not cache_parent.is_dir():
            raise ValueError('qualification_cache_parent_required')
        cache_root = cache_parent / ('root-start-' + uuid4().hex)
        guard(cache_root)
        cache_root.mkdir(exist_ok=False)
        guard(cache_root)
        acquisition = ComponentAcquisition(cache_root=cache_root,
            cache_owner='encyclopedia-sealed-qualification', opener=SealedEncyclopediaFixtureOpener(),
            host_version=INPUT['host_version'], trust_bytes=TRUST, trust_sha256=INPUT['trust_sha256'],
            max_artifact_bytes=MAX_ARCHIVE)
    # Reader/custody cannot create cache state before inherited admission.
    return StandaloneEntryProfile(ROOT, ROOT / INPUT['data_relative'], ROOT / INPUT['settings_relative'],
        bootstrap_pin, TRUST, INPUT['trust_sha256'], INPUT['channel'], acquisition)

'''


def render_fixture_profile(value):
    value = validate_profile(value)
    source = 'INPUT = ' + repr(value) + '\n' + RUNTIME
    ast.parse(source)
    return source.encode('utf-8')


def render_entry(role, *, bound):
    require(role in {'root', 'reader'} and type(bound) is bool, 'fixed_encyclopedia_recipe_role_required')
    if not bound:
        return b"raise RuntimeError('encyclopedia_qualification_profile_not_admitted')\n"
    root_guard = ("if sys.argv[1:] and tuple(sys.argv[1:]) != ('--encyclopedia-custody',):\n"
                  "    raise RuntimeError('fixed_qualification_root_role_required')\n")
    child_guard = ("if not any(a.split('=',1)[0] in HOST for a in sys.argv[1:]):\n"
                   "    raise RuntimeError('fixed_qualification_reader_role_required')\n")
    profile_assignment = ("if tuple(sys.argv[1:]) == ('--encyclopedia-custody',):\n"
        "    bootstrap_entry.PROFILE = make_profile('custody')\n"
        "else:\n"
        "    from pathlib import Path\n"
        "    from encyclopedia_fixture_profile import ROOT\n"
        "    from suite_core.discovery.component_staging import guard\n"
        "    guard(ROOT)\n"
        "    if Path(sys.executable).absolute() != ROOT / 'TagEncyclopedia.exe':\n"
        "        raise ValueError('fixed_qualification_root_layout_required')\n"
        "    try:\n        bootstrap_entry.PROFILE = make_profile('root')\n"
        "    except Exception as error:\n"
        "        from suite_core.encyclopedia.__main__ import report_preowner_startup_failure\n"
        "        raise SystemExit(report_preowner_startup_failure(error, root=ROOT))\n"
        if role == 'root' else "bootstrap_entry.PROFILE = make_profile('reader')\n")
    return ("# Generated sealed fixture entry; not a production profile.\nimport sys\n"
        "if not getattr(sys, 'frozen', False):\n    raise RuntimeError('frozen_fixture_entry_required')\n"
        "from encyclopedia_fixture_profile import make_profile\n"
        "from suite_core.encyclopedia import bootstrap_entry\n"
        "from suite_core.encyclopedia.entry_binding import HOST\n" +
        (root_guard if role == 'root' else child_guard) +
        profile_assignment +
        "from suite_core.encyclopedia.__main__ import run\n"
        "if __name__ == '__main__':\n    raise SystemExit(run())\n").encode('utf-8')


NORMAL_RUNTIME = r'''
from pathlib import Path
from hashlib import sha256
from uuid import uuid4
import argparse
import os
import sys
from core.component_acquisition import ComponentAcquisition
from core.component_release_transport import ComponentReleaseTransport, ReleaseTransportPolicy
from core.encyclopedia_enrollment import EncyclopediaEnrollmentContext, compiled_enrollment_needed
from core.encyclopedia_update_binding import admit_standalone_binding
from suite_core.discovery.component_staging import guard, canonical, MAX_ARCHIVE
from suite_core.discovery.component_release_signature import _trust
from suite_core.installation import SuiteLayout, InstallMode, _path
from suite_core.encyclopedia.entry_binding import StandaloneEntryProfile, HOST
from release_contract import sha256_file

def _lexical_path(value):
    path = Path(value)
    if (not path.is_absolute() or len(str(path)) > 4096 or len(path.parts) > 128
            or '..' in path.parts or str(path).startswith(('//', chr(92) * 2))):
        raise ValueError('local_absolute_normal_path_required')
    return path

def _local_drive(path):
    if os.name == 'nt':
        import ctypes
        # UNC/device paths were refused lexically; mapped network drives also hold.
        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) not in (2, 3, 6):
            raise ValueError('local_normal_drive_required')

def make_profile(role):
    # Compiled public input pin precedes any path observation or cache creation.
    if sha256(canonical(INPUT)).hexdigest() != INPUT_SHA256:
        raise ValueError('compiled_normal_input_changed')
    trust = bytes.fromhex(INPUT['trust_hex'])
    if sha256(trust).hexdigest() != INPUT['trust_sha256']:
        raise ValueError('compiled_normal_trust_changed')
    _trust(trust)
    if role not in ('root', 'reader', 'custody') or not getattr(sys, 'frozen', False):
        raise ValueError('fixed_frozen_normal_role_required')
    actual = _lexical_path(sys.executable)
    _local_drive(actual)
    arguments = tuple(sys.argv[1:])
    if actual.name != 'TagEncyclopedia.exe':
        raise ValueError('actual_normal_program_required')
    if role == 'reader':
        if any(sum(a.split('=', 1)[0] == key for a in arguments) != 1 for key in HOST):
            raise ValueError('exact_normal_reader_locators_required')
        if any(a.startswith('--encyclopedia-') and a.split('=', 1)[0] not in HOST for a in arguments):
            raise ValueError('exact_normal_reader_locators_required')
        parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        for key in HOST: parser.add_argument(key, required=True)
        parsed, _renderer = parser.parse_known_args(arguments)
        root = _lexical_path(parsed.encyclopedia_code_root)
        data = _lexical_path(parsed.encyclopedia_data_root)
        settings = _lexical_path(parsed.encyclopedia_settings)
        if (data != root / 'Data' or settings != data / 'Encyclopedia/settings.json'
                or not actual.is_relative_to(root / 'Apps/collectigen.tag-encyclopedia')):
            raise ValueError('actual_normal_reader_layout_required')
        # No filesystem access before the existing signed/inherited child admission.
        return StandaloneEntryProfile(root, data, settings, None, trust,
            INPUT['trust_sha256'], INPUT['channel'], None)
    if (role == 'root' and arguments or role == 'custody' and arguments != ('--encyclopedia-custody',)):
        raise ValueError('exact_normal_root_argv_required')
    root = actual.parent
    if any(parent.name.casefold() == 'collectigen.tag-encyclopedia' and parent.parent.name.casefold() == 'apps'
            for parent in (root, *root.parents)):
        raise ValueError('normal_root_in_immutable_reader_refused')
    # Guard original spellings BEFORE _path resolves them. Reject aliases, not normalize them.
    guard(actual)
    if _path(actual) != actual or _path(root) != root or not actual.is_file():
        raise ValueError('actual_normal_root_layout_required')
    data, settings = root / 'Data', root / 'Data/Encyclopedia/settings.json'
    SuiteLayout(root, data, InstallMode.PORTABLE)
    bootstrap_pin = sha256_file(actual)
    if role == 'custody':
        # Stock custody admission requires this observed image digest; no Data/cache access.
        return StandaloneEntryProfile(root, data, settings, bootstrap_pin, trust,
            INPUT['trust_sha256'], INPUT['channel'], None)
    context = EncyclopediaEnrollmentContext(root, data, settings, bootstrap_pin, trust, INPUT['trust_sha256'])
    if not compiled_enrollment_needed(context):
        # Existing/corrupt/relocated state must pass stock read-only signed admission.
        # Never convert such a tree to fresh setup or rewrite its absolute origin.
        admit_standalone_binding(root=root, data_root=data, settings_path=settings,
            bootstrap_sha256=bootstrap_pin, trust_bytes=trust, trust_sha256=INPUT['trust_sha256'])
    opener = ComponentReleaseTransport(ReleaseTransportPolicy(tuple(INPUT['asset_redirect_origins']), 4))
    for directory in (data, data / 'Encyclopedia', data / 'Encyclopedia/UpdateCache'):
        guard(directory)
        if not directory.exists(): directory.mkdir(exist_ok=False)
        guard(directory)
        if not directory.is_dir() or _path(directory) != directory:
            raise ValueError('normal_cache_ancestor_directory_required')
    cache_parent = data / 'Encyclopedia/UpdateCache'
    cache_root = cache_parent / ('root-start-' + uuid4().hex)
    guard(cache_root)
    cache_root.mkdir(exist_ok=False)
    guard(actual)
    guard(cache_root)
    if (not cache_root.is_dir() or _path(cache_root) != cache_root
            or sha256_file(actual) != bootstrap_pin):
        raise ValueError('normal_cache_postcreation_changed')
    acquisition = ComponentAcquisition(cache_root=cache_root, cache_owner='encyclopedia-normal-portable',
        opener=opener, host_version=INPUT['host_version'], trust_bytes=trust,
        trust_sha256=INPUT['trust_sha256'], max_artifact_bytes=MAX_ARCHIVE)
    return StandaloneEntryProfile(root, data, settings, bootstrap_pin, trust,
        INPUT['trust_sha256'], INPUT['channel'], acquisition)
'''


def render_normal_profile(value):
    from suite_core.discovery.component_staging import canonical
    value = validate_normal_profile(value)
    source = ('# Reviewed normal portable public inputs; no source activation.\nINPUT = ' + repr(value)
        + '\nINPUT_SHA256 = ' + repr(sha256(canonical(value)).hexdigest()) + '\n' + NORMAL_RUNTIME)
    ast.parse(source)
    return source.encode('utf-8')


def render_normal_entry(role):
    require(role in {'root', 'reader'}, 'fixed_encyclopedia_recipe_role_required')
    assignment = ("role = 'custody' if tuple(sys.argv[1:]) == ('--encyclopedia-custody',) else 'root'\n"
        if role == 'root' else "role = 'reader'\n")
    source = ("# Reviewed ordinary entry; no qualification driver.\nimport sys\n"
        "from encyclopedia_compiled_profile import make_profile\n"
        "from suite_core.encyclopedia import bootstrap_entry\n" + assignment +
        "bootstrap_entry.PROFILE = make_profile(role)\n"
        "from suite_core.encyclopedia.__main__ import run\n"
        "if __name__ == '__main__':\n    raise SystemExit(run())\n")
    ast.parse(source)
    return source.encode('utf-8')
