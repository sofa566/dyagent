from __future__ import annotations

import json
from pathlib import Path

from src.services.skill_registry import SkillRegistry, SkillRuleRouter


def test_build_manifest_from_markdown_extracts_core_fields() -> None:
    markdown = """---
name: sample-skill
description: >
  建立 PDF 文件。
triggers:
  - pdf
  - report
---

# sample-skill

| User intent | Route | Scripts used |
|---|---|---|
| Generate a new PDF | CREATE | make.py |

Rule: when in doubt ask user.
"""
    registry = SkillRegistry(roots=[])
    manifest = registry.build_manifest_from_markdown(
        skill_md_text=markdown,
        source_id='inline:sample',
    )

    assert manifest.name == 'sample-skill'
    assert 'pdf' in manifest.triggers
    assert 'report' in manifest.triggers
    assert manifest.routes
    assert manifest.routes[0].route == 'CREATE'
    assert manifest.constraints


def test_list_manifests_respects_file_cache_invalidation(tmp_path: Path) -> None:
    skill_dir = tmp_path / 'skills' / 'demo'
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / 'SKILL.md'
    skill_file.write_text('# demo\n\nfirst description', encoding='utf-8')

    registry = SkillRegistry(roots=[tmp_path / 'skills'])
    first = registry.list_manifests()
    assert len(first) == 1
    assert first[0].description == 'first description'

    skill_file.write_text('# demo\n\nsecond description', encoding='utf-8')
    second = registry.list_manifests()
    assert len(second) == 1
    assert second[0].description == 'second description'


def test_rule_router_matches_cases_from_fixture() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    fixture_path = repo_root / 'backend' / 'tests' / 'fixtures' / 'skill_router_cases.json'
    cases = json.loads(fixture_path.read_text(encoding='utf-8'))

    registry = SkillRegistry(roots=[])
    router = SkillRuleRouter()

    pdf_md = (repo_root / 'tools' / 'out_pdf.json').read_text(encoding='utf-8')
    docx_md = (repo_root / 'tools' / 'out_docx.json').read_text(encoding='utf-8')
    humanizer_md = (repo_root / 'tools' / 'humanizer-zh-tw' / 'SKILL.md').read_text(encoding='utf-8')

    import json as _json

    pdf_source = _json.loads(pdf_md).get('source_markdown') or ''
    docx_source = _json.loads(docx_md).get('source_markdown') or ''

    manifests = [
        registry.build_manifest_from_markdown(skill_md_text=pdf_source, source_id='fixture:minimax-pdf'),
        registry.build_manifest_from_markdown(skill_md_text=docx_source, source_id='fixture:minimax-docx'),
        registry.build_manifest_from_markdown(skill_md_text=humanizer_md, source_id='fixture:humanizer-zh-tw'),
    ]

    correct = 0
    for case in cases:
        result = router.match(message=str(case['message']), manifests=manifests)
        if str(result.get('skill_name') or '') == str(case['expected_skill']):
            correct += 1

    accuracy = correct / max(1, len(cases))
    assert accuracy >= 0.9
