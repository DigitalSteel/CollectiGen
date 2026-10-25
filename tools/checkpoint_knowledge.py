"""Adapt committed/shipped defaults into a supplemental change-review report."""
from __future__ import annotations

from contextlib import contextmanager
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import subprocess

DEFAULT_FILES = (
    'foundation_tags.json','community_tags.json','promoted_tags.json','tag_aliases.json',
    'series.json','characters.json','outfits.json','relationships.json','categories.json',
)
EVIDENCE_PATTERNS = ('tools/tag-semantic-audits', 'tools/tag-identity-reviews',
                     'data/defaults/character_variant_reviews.json')


@contextmanager
def synthetic_profile(root):
    values={'TAG_MANAGER_PYTEST':'1','TAG_MANAGER_PYTEST_USER_DATA_DIR':str(root/'synthetic-user'),
            'TAG_MANAGER_PYTEST_CACHE_DIR':str(root/'synthetic-cache')}
    before={key:os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key,value in before.items():
            if value is None: os.environ.pop(key,None)
            else: os.environ[key]=value


def read_catalog(defaults, evidence=None):
    # Use the application's canonical layering/retirement/alias resolver.
    from core.default_sync import load_effective_tag_defaults
    effective=load_effective_tag_defaults(defaults)
    def payload(name):
        path=defaults/name
        return json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
    relationships=payload('relationships.json').get('relationships',[])
    return {
        'tags':[dict(item) for item in effective.tags],
        'sources':payload('series.json').get('series',[]),
        'characters':payload('characters.json').get('characters',[]),
        'outfits':payload('outfits.json').get('outfits',[]),
        'relationships':[row for row in relationships if not row.get('disabled')],
        'evidence_fingerprints':evidence or {},
        'effective_default_findings':[dataclasses.asdict(item) if dataclasses.is_dataclass(item) else str(item)
                                      for item in effective.findings],
    }


def review_changes(root: Path, base: str, workspace: Path, reviews_path: str | None = None, explicit_paths=()):
    from tools.knowledge_change_review import evaluate_changes
    from tools.test_checkpoint import source_path
    catalogs=[]
    # Git lists only approved shipped defaults/review evidence; no user layer.
    tracked=subprocess.check_output(['git','ls-tree','-r','--name-only','-z',base,'--',
                                     'data/defaults',*EVIDENCE_PATTERNS],cwd=root)
    baseline_paths={item.decode('utf-8') for item in tracked.split(b'\0') if item}
    current_evidence=subprocess.check_output(['git','ls-files','-z','--',*EVIDENCE_PATTERNS],cwd=root)
    evidence_paths={p for p in baseline_paths if p.startswith(('tools/tag-semantic-audits/','tools/tag-identity-reviews/'))
                    or p=='data/defaults/character_variant_reviews.json'}
    evidence_paths.update(item.decode('utf-8') for item in current_evidence.split(b'\0') if item)
    evidence_paths.update(p for p in explicit_paths if p.startswith((
        'tools/tag-semantic-audits/', 'tools/tag-identity-reviews/')))
    with synthetic_profile(workspace):
        for label in ('before','after'):
            defaults=workspace/label
            defaults.mkdir(parents=True,exist_ok=False)
            for name in DEFAULT_FILES:
                relative='data/defaults/'+name
                path=source_path(root,relative)
                if label=='before':
                    data=subprocess.check_output(['git','show',f'{base}:{relative}'],cwd=root) if relative in baseline_paths else None
                else:
                    data=path.read_bytes() if path.is_file() else None
                if data is not None: (defaults/name).write_bytes(data)
            evidence={}
            for relative in sorted(evidence_paths):
                path=source_path(root,relative)
                if label=='before':
                    data=subprocess.check_output(['git','show',f'{base}:{relative}'],cwd=root) if relative in baseline_paths else None
                else:
                    data=path.read_bytes() if path.is_file() else None
                if data is not None: evidence[relative]=hashlib.sha256(data).hexdigest()
            catalogs.append(read_catalog(defaults,evidence))
    receipts=[]
    if reviews_path:
        from tools.test_suites import normalize_requested_paths
        relative=normalize_requested_paths([reviews_path],root)[0]
        payload=json.loads(source_path(root,relative).read_text(encoding='utf-8-sig'))
        if not isinstance(payload,dict) or payload.get('schema_version')!=1 or not isinstance(payload.get('reviews'),list):
            raise ValueError('Knowledge reviews require schema_version 1 and a reviews list.')
        receipts=payload['reviews']
    report=evaluate_changes(*catalogs,receipts)
    report['default_resolution_findings']=catalogs[1]['effective_default_findings']
    before_findings=catalogs[0]['effective_default_findings']
    for finding in catalogs[1]['effective_default_findings']:
        issue={'code':'default_resolution','message':str(finding)}
        report['issues'].append(issue)
        if finding not in before_findings: report['new_issues'].append(issue)
    report['base_commit']=base
    report['authority']='Supplemental product-fit/completeness review; existing reviewed defaults and semantic evidence remain authoritative.'
    return report
