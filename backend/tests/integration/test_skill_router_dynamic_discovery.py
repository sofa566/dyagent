from __future__ import annotations

from pathlib import Path

from src.services.skill_registry import SkillRegistry, SkillRuleRouter


def _write_skill(skill_dir: Path, *, name: str, description: str, body: str) -> None:
    content = (
        '---\n'
        f'name: {name}\n'
        f'description: {description}\n'
        '---\n\n'
        f'# {name}\n\n'
        f'{body}\n'
    )
    (skill_dir / 'SKILL.md').write_text(content, encoding='utf-8')


def test_dynamic_discovery_matches_new_skill_without_router_code_change(tmp_path: Path) -> None:
    skills_root = tmp_path / 'skills'
    skills_root.mkdir(parents=True)

    existing_skill_dir = skills_root / 'existing'
    existing_skill_dir.mkdir(parents=True)
    _write_skill(
        existing_skill_dir,
        name='existing-summary',
        description='Summarize text.',
        body='Use this skill when users ask to summarize content.',
    )

    registry = SkillRegistry(roots=[skills_root])
    router = SkillRuleRouter()

    manifests_before = registry.list_manifests()
    assert any(manifest.name == 'existing-summary' for manifest in manifests_before)

    new_skill_dir = skills_root / 'new-humanizer'
    new_skill_dir.mkdir(parents=True)
    _write_skill(
        new_skill_dir,
        name='fresh-humanizer',
        description='去除 AI 味與人性化潤稿',
        body='Use this skill for "潤稿", "人性化", "去 AI 味" requests.',
    )

    manifests_after = registry.list_manifests()
    result = router.match(
        message='請幫我把這段文字去 AI 味並潤稿',
        manifests=manifests_after,
        candidate_skill_names=['fresh-humanizer'],
    )

    assert result['skill_name'] == 'fresh-humanizer'
    assert int(result['score'] or 0) > 0
