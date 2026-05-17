from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.core.config import settings
from src.core.logging import get_logger

_log = get_logger('skill_registry')


@dataclass
class SkillRoute:
    route: str
    user_intent: str


@dataclass
class SkillManifest:
    name: str
    description: str
    triggers: list[str] = field(default_factory=list)
    routes: list[SkillRoute] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    source_id: str = ''
    source_hash: str = ''
    source_path: str = ''
    source_markdown: str = ''


class SkillRegistry:
    # 目的：集中提供 SKILL.md 的掃描、解析與快取能力。
    # 為什麼：路由層需要以 SKILL.md 為唯一真相，避免先轉 JSON 造成資訊落差與維護成本。
    def __init__(self, roots: list[Path] | None = None) -> None:
        self._roots = roots or _resolve_registry_roots()
        self._file_cache: dict[str, tuple[str, SkillManifest]] = {}
        self._markdown_cache: dict[str, SkillManifest] = {}

    def list_manifests(self) -> list[SkillManifest]:
        # 目的：回傳目前可用的所有技能 manifest。
        # 為什麼：讓路由器可動態讀取技能集合，新增 SKILL.md 時不需改程式碼。
        manifests: list[SkillManifest] = []
        for skill_md_path in self._discover_skill_files():
            manifest = self._load_manifest_from_file(skill_md_path)
            if manifest is not None:
                manifests.append(manifest)
        return manifests

    def build_manifest_from_markdown(self, *, skill_md_text: str, source_id: str, source_path: str = '') -> SkillManifest:
        # 目的：將單一 SKILL.md 內容解析為最小 manifest。
        # 為什麼：聊天路由需即時以 markdown 內容推斷能力，不應依賴預轉檔流程。
        normalized_text = str(skill_md_text or '').strip()
        content_hash = hashlib.sha256(normalized_text.encode('utf-8')).hexdigest()
        cache_key = f"{source_id}:{content_hash}"
        cached = self._markdown_cache.get(cache_key)
        if cached is not None:
            return cached

        manifest = _parse_skill_markdown(
            skill_md_text=normalized_text,
            source_id=source_id,
            source_path=source_path,
            source_hash=content_hash,
        )
        self._markdown_cache[cache_key] = manifest
        return manifest

    def _discover_skill_files(self) -> list[Path]:
        discovered: list[Path] = []
        seen: set[str] = set()
        for root in self._roots:
            if not root.exists() or not root.is_dir():
                continue
            for path in root.rglob('SKILL.md'):
                resolved = str(path.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                discovered.append(path)
        return sorted(discovered, key=lambda p: str(p))

    def _load_manifest_from_file(self, skill_md_path: Path) -> SkillManifest | None:
        try:
            stat = skill_md_path.stat()
            signature = f"{stat.st_mtime_ns}:{stat.st_size}"
            cached = self._file_cache.get(str(skill_md_path))
            if cached is not None and cached[0] == signature:
                return cached[1]

            text = skill_md_path.read_text(encoding='utf-8').strip()
            manifest = self.build_manifest_from_markdown(
                skill_md_text=text,
                source_id=str(skill_md_path.resolve()),
                source_path=str(skill_md_path.resolve()),
            )
            self._file_cache[str(skill_md_path)] = (signature, manifest)
            return manifest
        except Exception as error:
            _log.warning('skill_registry.load_manifest_failed', path=str(skill_md_path), error=str(error))
            return None


class SkillRuleRouter:
    # 目的：使用 manifest 規則計分來挑選最適技能。
    # 為什麼：不硬編碼特定技能名稱，新增 SKILL.md 後可自動進入路由決策。
    def match(
        self,
        *,
        message: str,
        manifests: list[SkillManifest],
        candidate_skill_names: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_message = str(message or '').strip().lower()
        if not normalized_message:
            return {'skill_name': None, 'score': 0, 'matched_rules': []}

        allowed_names = {str(name or '').strip().lower() for name in (candidate_skill_names or []) if str(name or '').strip()}
        best_name: str | None = None
        best_score = 0
        best_rules: list[dict[str, Any]] = []

        for manifest in manifests:
            manifest_name = str(manifest.name or '').strip()
            if not manifest_name:
                continue
            if allowed_names and manifest_name.lower() not in allowed_names:
                continue

            score, matched_rules = _score_manifest(manifest=manifest, normalized_message=normalized_message)
            if score > best_score:
                best_name = manifest_name
                best_score = score
                best_rules = matched_rules

        return {
            'skill_name': best_name,
            'score': best_score,
            'matched_rules': best_rules,
        }


def _resolve_registry_roots() -> list[Path]:
    # 目的：解析技能掃描根目錄列表。
    # 為什麼：以設定驅動來源路徑，避免路由邏輯耦合固定專案結構。
    configured = str(getattr(settings, 'SKILL_REGISTRY_PATHS', '') or '').strip()
    configured_items = [item.strip() for item in configured.split(',') if item.strip()]
    if not configured_items:
        configured_items = ['tools', '.opencode/skills']

    repo_root = Path(__file__).resolve().parents[3]
    roots: list[Path] = []
    for item in configured_items:
        candidate = Path(item)
        if not candidate.is_absolute():
            candidate = (repo_root / candidate).resolve()
        roots.append(candidate)
    return roots


def _parse_skill_markdown(*, skill_md_text: str, source_id: str, source_path: str, source_hash: str) -> SkillManifest:
    content = str(skill_md_text or '').strip()
    metadata: dict[str, Any] = {}
    body = content

    front_matter = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', content, flags=re.DOTALL)
    if front_matter is not None:
        metadata = _safe_load_yaml(str(front_matter.group(1) or ''))
        body = str(front_matter.group(2) or '').strip()

    name = str(metadata.get('name') or '').strip()
    if not name:
        heading = re.search(r'^#\s+(.+)$', body, flags=re.MULTILINE)
        if heading is not None:
            name = str(heading.group(1) or '').strip()

    description = str(metadata.get('description') or '').strip()
    if not description:
        description = _first_meaningful_line(body)

    triggers = _parse_triggers(metadata=metadata, body=body, name=name, description=description)
    routes = _parse_routes(body)
    constraints = _parse_constraints(body)

    return SkillManifest(
        name=name,
        description=description,
        triggers=triggers,
        routes=routes,
        constraints=constraints,
        source_id=source_id,
        source_hash=source_hash,
        source_path=source_path,
        source_markdown=content,
    )


def _safe_load_yaml(text: str) -> dict[str, Any]:
    try:
        import yaml
    except Exception:
        return {}
    try:
        loaded = yaml.safe_load(text) or {}
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _first_meaningful_line(text: str) -> str:
    for line in str(text or '').splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        return stripped
    return ''


def _parse_triggers(*, metadata: dict[str, Any], body: str, name: str, description: str) -> list[str]:
    # 目的：萃取技能觸發詞與意圖片語。
    # 為什麼：路由可直接使用 trigger 規則，降低僅靠 description 的誤判率。
    triggers: set[str] = set()

    raw_triggers = metadata.get('triggers')
    if isinstance(raw_triggers, list):
        for item in raw_triggers:
            text = str(item or '').strip().lower()
            if _is_valid_trigger_token(text):
                triggers.add(text)

    metadata_block = metadata.get('metadata')
    if isinstance(metadata_block, dict):
        maybe_trigger = str(metadata_block.get('trigger') or '').strip().lower()
        if _is_valid_trigger_token(maybe_trigger):
            triggers.add(maybe_trigger)

    quoted_patterns = [
        r'"([^"]{2,60})"',
        r'「([^」]{2,60})」',
        r'“([^”]{2,60})”',
    ]
    quoted_source = f"{description}\n{body}"
    for pattern in quoted_patterns:
        for matched in re.findall(pattern, quoted_source):
            candidate = str(matched or '').strip().lower()
            if _is_valid_trigger_token(candidate):
                triggers.add(candidate)

    name_text = str(name or '').strip().lower()
    if name_text:
        triggers.add(name_text)
        for token in re.split(r'[-_\s]+', name_text):
            cleaned = token.strip()
            if _is_valid_trigger_token(cleaned):
                triggers.add(cleaned)

    cjk_source = f"{description}\n" + '\n'.join(str(body or '').splitlines()[:80])
    for term in re.findall(r'[\u4e00-\u9fff]{2,12}', cjk_source):
        cleaned_term = str(term or '').strip().lower()
        if _is_valid_trigger_token(cleaned_term):
            triggers.add(cleaned_term)

    try:
        import jieba  # type: ignore

        for piece in jieba.lcut(cjk_source):
            cleaned_piece = str(piece or '').strip().lower()
            if _is_valid_trigger_token(cleaned_piece):
                triggers.add(cleaned_piece)
    except Exception:
        pass

    return sorted(triggers, key=len, reverse=True)


def _parse_routes(body: str) -> list[SkillRoute]:
    routes: list[SkillRoute] = []
    for line in str(body or '').splitlines():
        stripped = line.strip()
        if not stripped.startswith('|'):
            continue
        if re.match(r'^\|\s*-+\s*\|', stripped):
            continue
        cells = [cell.strip() for cell in stripped.strip('|').split('|')]
        if len(cells) < 2:
            continue
        user_intent = str(cells[0] or '').strip()
        route = str(cells[1] or '').strip()
        if not user_intent or not route:
            continue
        if route.lower() in {'route', 'scripts used'}:
            continue
        routes.append(SkillRoute(route=route, user_intent=user_intent))
    return routes


def _parse_constraints(body: str) -> list[str]:
    constraints: list[str] = []
    for line in str(body or '').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith('rule:') or lowered.startswith('must ') or '不得' in stripped or '必須' in stripped:
            constraints.append(stripped)
    return constraints[:20]


def _score_manifest(*, manifest: SkillManifest, normalized_message: str) -> tuple[int, list[dict[str, Any]]]:
    matched_rules: list[dict[str, Any]] = []
    score = 0

    name_weight = max(1, int(getattr(settings, 'SKILL_ROUTER_NAME_WEIGHT', 4) or 4))
    trigger_weight = max(1, int(getattr(settings, 'SKILL_ROUTER_TRIGGER_WEIGHT', 3) or 3))
    route_intent_weight = max(1, int(getattr(settings, 'SKILL_ROUTER_ROUTE_INTENT_WEIGHT', 2) or 2))
    description_weight = max(1, int(getattr(settings, 'SKILL_ROUTER_DESCRIPTION_WEIGHT', 1) or 1))
    token_score_ceiling = max(2, int(getattr(settings, 'SKILL_ROUTER_MAX_TOKEN_SCORE_LEN', 12) or 12))

    def _accumulate(tokens: list[str], *, weight: int, rule_type: str) -> None:
        nonlocal score
        seen_tokens: set[str] = set()
        for token in tokens:
            normalized_token = str(token or '').strip().lower()
            if normalized_token in seen_tokens:
                continue
            seen_tokens.add(normalized_token)
            if not _is_valid_trigger_token(normalized_token):
                continue
            if normalized_token in normalized_message:
                token_score = min(len(normalized_token), token_score_ceiling) * weight
                score += token_score
                matched_rules.append({'type': rule_type, 'token': normalized_token, 'score': token_score})

    name_tokens = [manifest.name] + [part for part in re.split(r'[-_\s]+', str(manifest.name or '')) if part]
    _accumulate(name_tokens, weight=name_weight, rule_type='name')
    _accumulate(manifest.triggers, weight=trigger_weight, rule_type='trigger')
    _accumulate([route.user_intent for route in manifest.routes], weight=route_intent_weight, rule_type='route_intent')

    description_tokens = re.findall(r'[\u4e00-\u9fff]{2,}|[a-zA-Z][a-zA-Z0-9_-]{2,}', str(manifest.description or '').lower())
    _accumulate(description_tokens[:20], weight=description_weight, rule_type='description')

    manifest_blob = ' '.join(
        [
            str(manifest.name or '').lower(),
            str(manifest.description or '').lower(),
            ' '.join([str(t or '').lower() for t in manifest.triggers[:50]]),
        ]
    )

    format_boost = max(1, int(getattr(settings, 'SKILL_ROUTER_FORMAT_BOOST', 20) or 20))
    format_boost_rules = [
        ('pdf', _parse_csv_hints(getattr(settings, 'SKILL_ROUTER_PDF_HINTS', 'pdf'))),
        ('docx', _parse_csv_hints(getattr(settings, 'SKILL_ROUTER_DOCX_HINTS', 'docx,word'))),
        ('humanizer', _parse_csv_hints(getattr(settings, 'SKILL_ROUTER_HUMANIZER_HINTS', '潤稿,人性化,去 ai 味'))),
    ]
    for boost_type, hints in format_boost_rules:
        if not any(str(hint).lower() in normalized_message for hint in hints):
            continue
        if boost_type == 'pdf' and 'pdf' in manifest_blob:
            score += format_boost
            matched_rules.append({'type': 'format_boost', 'token': 'pdf', 'score': format_boost})
        elif boost_type == 'docx' and any(k in manifest_blob for k in ['docx', 'word', '文档', '公文', '合同']):
            score += format_boost
            matched_rules.append({'type': 'format_boost', 'token': 'docx', 'score': format_boost})
        elif boost_type == 'humanizer' and any(k in manifest_blob for k in ['humanizer', '人性化', 'ai 寫作痕跡', 'ai 生成痕跡']):
            score += format_boost
            matched_rules.append({'type': 'format_boost', 'token': 'humanizer', 'score': format_boost})

    return score, matched_rules


def _parse_csv_hints(raw_text: Any) -> list[str]:
    values = [str(item).strip().lower() for item in str(raw_text or '').split(',') if str(item).strip()]
    return list(dict.fromkeys(values))


def _is_valid_trigger_token(token: str) -> bool:
    cleaned = str(token or '').strip().lower()
    if len(cleaned) < 2:
        return False

    ascii_stopwords = {
        'to', 'of', 'in', 'on', 'at', 'by', 'for', 'the', 'a', 'an',
        'this', 'that', 'these', 'those', 'and', 'or', 'with', 'from',
    }
    cjk_stopwords = {
        '以及', '相關', '功能', '系統', '提供', '支援', '工具', '技能', '處理',
    }

    if re.fullmatch(r'[a-z][a-z0-9_-]*', cleaned):
        if cleaned in ascii_stopwords:
            return False
        return len(cleaned) >= 3
    if re.fullmatch(r'[\u4e00-\u9fff]{2,}', cleaned):
        return cleaned not in cjk_stopwords
    return True


@lru_cache(maxsize=1)
def get_skill_registry() -> SkillRegistry:
    return SkillRegistry()


@lru_cache(maxsize=1)
def get_skill_rule_router() -> SkillRuleRouter:
    return SkillRuleRouter()
