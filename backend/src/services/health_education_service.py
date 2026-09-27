from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.api.errors import not_found_error, validation_error
from src.core.config import settings
from src.core.logging import get_logger
from src.models import (
    HealthEducationContent,
    HealthEducationDeliveryLog,
    HealthEducationSourceRule,
    LineChannelSession,
    RenalPatient,
    User,
)
from src.models.renal_care import ScheduledTaskTemplate
from src.services.chat_router import ChatRouter
from src.services.llm_client import LLMClient

logger = get_logger(__name__)

DEFAULT_SCHEDULER_TEMPLATE_KEY = 'health.education.dispatch'
ALLOWED_CONTENT_STATUS = {'draft', 'approved', 'rejected', 'sent'}
ALLOWED_AUDIENCE_RULES = {'all', 'diabetic_only', 'non_diabetic_only'}
SOURCE_POLICY_TRUSTED = 'trusted'
SOURCE_POLICY_REVIEW_REQUIRED = 'review_required'
SOURCE_POLICY_BLOCKED = 'blocked'
DEFAULT_IMPORT_SOURCE_NAME = 'mcp-firecrawl-search'
DEFAULT_IMPORT_TOPIC = '請搜尋近期可用於腎友的衛教重點'
DEFAULT_IMPORT_LIMIT = 5
MAX_IMPORT_LIMIT = 10
MAX_AGENT_OUTPUT_DEBUG_CHARS = 2000
MAX_IMPORT_SKIP_DETAILS = 20
DEFAULT_DISPATCH_COOLDOWN_DAYS = 30
MAX_DISPATCH_COOLDOWN_DAYS = 180
MCP_SEARCH_TOOL_NAME = 'firecrawl-mcp'
MCP_SEARCH_TARGET_TOOL_NAME = 'firecrawl_search'
PLACEHOLDER_SOURCE_DOMAINS = {
    'example.com',
    'example.org',
    'example.net',
    'localhost',
}
CHINESE_DOMAIN_SUFFIXES = ('.tw', '.台灣', '.台湾', '.hk', '.mo')
CHINESE_PATH_HINTS = ('/zh', '/zh-tw', '/tw', '/taiwan', '/chinese')


class HealthEducationService:
    # 目的：管理衛教內容審核與發送流程。
    # 為什麼：把內容治理（draft/approved）與投遞稽核集中在服務層，避免路由直接拼湊業務。

    def create_content(self, *, db: Session, payload: dict, current_user: User | None) -> dict:
        # 目的：建立衛教候選內容。
        # 為什麼：讓 Agent 或人工蒐集結果能先落在 draft，經審核後再發送。
        normalized_payload = self._normalize_content_payload(db=db, payload=payload)
        exists = db.query(HealthEducationContent).filter(HealthEducationContent.source_url == normalized_payload['source_url']).first()
        if exists is not None:
            raise validation_error('source_url 已存在，請勿重複新增')
        row = HealthEducationContent(
            title=normalized_payload['title'],
            source_name=normalized_payload['source_name'],
            source_url=normalized_payload['source_url'],
            summary=normalized_payload['summary'],
            tags=normalized_payload['tags'],
            status='draft',
            created_by_user_id=str(getattr(current_user, 'id', '') or '') or None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {'ok': True, 'item': self._serialize_content(db=db, row=row)}

    def list_contents(self, *, db: Session, status: str, page: int, page_size: int) -> dict:
        # 目的：依狀態分頁查詢衛教內容列表。
        # 為什麼：資料量增加後需維持列表可讀性，並避免一次載入過多內容造成頁面壓力。
        normalized_status = str(status or '').strip().lower()
        safe_page = max(1, int(page or 1))
        safe_page_size = max(1, min(int(page_size or 50), 200))
        query = db.query(HealthEducationContent)
        if normalized_status:
            if normalized_status not in ALLOWED_CONTENT_STATUS:
                raise validation_error('status 僅支援 draft/approved/rejected/sent')
            query = query.filter(HealthEducationContent.status == normalized_status)
        total_count = int(query.count() or 0)
        total_pages = max(1, (total_count + safe_page_size - 1) // safe_page_size) if total_count > 0 else 1
        safe_page = min(safe_page, total_pages)
        offset_value = (safe_page - 1) * safe_page_size
        rows = (
            query.order_by(HealthEducationContent.created_at.desc(), HealthEducationContent.id.desc())
            .offset(offset_value)
            .limit(safe_page_size)
            .all()
        )
        return {
            'ok': True,
            'items': [self._serialize_content(db=db, row=row) for row in rows],
            'page': safe_page,
            'page_size': safe_page_size,
            'total': total_count,
            'total_pages': total_pages,
        }

    def import_candidate_contents(self, *, db: Session, payload: dict, current_user: User | None) -> dict:
        # 目的：批次匯入 Agent 搜尋的候選內容。
        # 為什麼：降低人工逐筆貼入成本，讓審核流程可快速接住搜尋結果。
        if not isinstance(payload, dict):
            raise validation_error('payload 格式錯誤')
        raw_items = payload.get('items') if isinstance(payload.get('items'), list) else []
        if not raw_items:
            raise validation_error('items 不可為空')
        agent_name = str(payload.get('agent_name') or '').strip() or 'unknown-agent'

        created_items: list[dict] = []
        skipped_duplicate = 0
        skipped_blocked = 0
        skipped_invalid = 0
        skipped_unreachable = 0
        skipped_non_chinese = 0
        skipped_not_trusted = 0
        skipped_items: list[dict] = []
        trusted_only_mode = bool(getattr(settings, 'HEALTH_EDUCATION_TRUSTED_ONLY_MODE', False))

        def append_skipped_item(reason: str, raw_item: object) -> None:
            # 目的：記錄每筆略過資料的原因與關鍵欄位。
            # 為什麼：前端需顯示可讀的匯入結果，讓營運能快速判斷為何 5 筆只進 1 筆。
            if len(skipped_items) >= MAX_IMPORT_SKIP_DETAILS:
                return
            item_payload = raw_item if isinstance(raw_item, dict) else {}
            skipped_items.append(
                {
                    'reason': str(reason or '').strip() or 'invalid',
                    'title': str(item_payload.get('title') or '').strip(),
                    'source_url': str(item_payload.get('source_url') or '').strip(),
                }
            )

        for raw_item in raw_items:
            try:
                normalized_item = self._normalize_content_payload(db=db, payload=raw_item)
                normalized_item = self._normalize_import_item_for_patient_language(db=db, normalized_item=normalized_item)
                if normalized_item is None:
                    skipped_non_chinese += 1
                    append_skipped_item('non_chinese', raw_item)
                    continue
                source_policy = self._resolve_source_policy(db=db, source_url=normalized_item['source_url'])
                if source_policy == SOURCE_POLICY_BLOCKED:
                    skipped_blocked += 1
                    append_skipped_item('blocked', normalized_item)
                    continue
                if trusted_only_mode and source_policy != SOURCE_POLICY_TRUSTED:
                    skipped_not_trusted += 1
                    append_skipped_item('not_trusted', normalized_item)
                    continue
                exists = db.query(HealthEducationContent).filter(HealthEducationContent.source_url == normalized_item['source_url']).first()
                if exists is not None:
                    skipped_duplicate += 1
                    append_skipped_item('duplicate', normalized_item)
                    continue
                if not self._is_source_url_accessible(source_url=normalized_item['source_url']):
                    skipped_unreachable += 1
                    append_skipped_item('unreachable', normalized_item)
                    continue
                tags = list(normalized_item['tags'])
                tags.append(f'agent:{agent_name}')
                row = HealthEducationContent(
                    title=normalized_item['title'],
                    source_name=normalized_item['source_name'],
                    source_url=normalized_item['source_url'],
                    summary=normalized_item['summary'],
                    tags=tags,
                    status='draft',
                    created_by_user_id=str(getattr(current_user, 'id', '') or '') or None,
                )
                db.add(row)
                db.flush()
                created_items.append(self._serialize_content(db=db, row=row))
            except Exception:
                skipped_invalid += 1
                append_skipped_item('invalid', raw_item)
                continue

        db.commit()
        return {
            'ok': True,
            'agent_name': agent_name,
            'source_name': agent_name,
            'created': len(created_items),
            'skipped_duplicate': skipped_duplicate,
            'skipped_blocked': skipped_blocked,
            'skipped_invalid': skipped_invalid,
            'skipped_unreachable': skipped_unreachable,
            'skipped_non_chinese': skipped_non_chinese,
            'skipped_not_trusted': skipped_not_trusted,
            'skipped_items': skipped_items,
            'items': created_items,
        }

    def _apply_mcp_response_aliases(self, *, response_payload: dict) -> dict:
        # 目的：提供 MCP 命名與舊有 Agent 命名的相容回應欄位。
        # 為什麼：前端與外部整合仍可能讀取 agent_*，遷移期需避免破壞既有流程。
        mcp_raw_output_preview = str(response_payload.get('mcp_raw_output_preview') or response_payload.get('agent_raw_output_preview') or '').strip()
        mcp_retry_output_preview = str(response_payload.get('mcp_retry_output_preview') or response_payload.get('agent_retry_output_preview') or '').strip()
        mcp_error = str(response_payload.get('mcp_error') or response_payload.get('agent_error') or '').strip()

        response_payload['mcp_raw_output_preview'] = mcp_raw_output_preview
        response_payload['mcp_retry_output_preview'] = mcp_retry_output_preview
        if mcp_error:
            response_payload['mcp_error'] = mcp_error

        response_payload['agent_raw_output_preview'] = mcp_raw_output_preview
        response_payload['agent_retry_output_preview'] = mcp_retry_output_preview
        if mcp_error:
            response_payload['agent_error'] = mcp_error
        return response_payload

    def import_candidates_from_mcp(self, *, db: Session, payload: dict, current_user: User | None) -> dict:
        # 目的：以固定 MCP 搜尋流程匯入候選衛教內容。
        # 為什麼：避免流程依賴特定 Agent，讓營運可直接透過工具取得可追溯來源。
        if not isinstance(payload, dict):
            raise validation_error('payload 格式錯誤')
        source_name = str(payload.get('agent_name') or payload.get('source_name') or DEFAULT_IMPORT_SOURCE_NAME).strip() or DEFAULT_IMPORT_SOURCE_NAME
        search_topic = str(payload.get('topic') or DEFAULT_IMPORT_TOPIC).strip() or DEFAULT_IMPORT_TOPIC
        requested_limit = self._normalize_import_limit(raw_limit=payload.get('limit'))

        router = ChatRouter()
        mcp_search_context = self._collect_mcp_search_context(
            router=router,
            db=db,
            topic=search_topic,
            limit=requested_limit,
        )
        if mcp_search_context.get('items'):
            import_response = self.import_candidate_contents(
                db=db,
                payload={'agent_name': source_name, 'items': mcp_search_context.get('items')},
                current_user=current_user,
            )
            import_response['source_name'] = source_name
            import_response['topic'] = search_topic
            import_response['requested_limit'] = requested_limit
            import_response['parsed_items'] = len(mcp_search_context.get('items') or [])
            import_response['mcp_raw_output_preview'] = self._trim_debug_text(raw_text='（無 Agent 模式：直接使用 firecrawl-mcp 搜尋結果）')
            import_response['mcp_retry_output_preview'] = self._trim_debug_text(raw_text='')
            import_response['mcp_search'] = mcp_search_context
            return self._apply_mcp_response_aliases(response_payload=import_response)
        logger.warning(
            'health_education_mcp_import.no_candidates',
            topic=search_topic,
            requested_limit=requested_limit,
            mcp_search=mcp_search_context,
        )
        empty_import_response = {
            'ok': True,
            'source_name': source_name,
            'topic': search_topic,
            'requested_limit': requested_limit,
            'parsed_items': 0,
            'created': 0,
            'skipped_duplicate': 0,
            'skipped_blocked': 0,
            'skipped_invalid': 0,
            'skipped_unreachable': 0,
            'skipped_non_chinese': 0,
            'skipped_not_trusted': 0,
            'skipped_items': [],
            'items': [],
            'mcp_raw_output_preview': self._trim_debug_text(raw_text='（無 Agent 模式：MCP 搜尋未產生可匯入候選）'),
            'mcp_retry_output_preview': self._trim_debug_text(raw_text=''),
            'mcp_error': 'MCP 搜尋已完成，但未產生可匯入候選內容（可能是搜尋結果為空或回傳格式不符）。',
            'mcp_search': mcp_search_context,
        }
        return self._apply_mcp_response_aliases(response_payload=empty_import_response)

    def _is_source_url_accessible(self, *, source_url: str) -> bool:
        # 目的：確認候選來源連結可被實際存取。
        # 為什麼：避免匯入不可開啟或示範用網址，降低營運後續清理成本。
        domain = self._extract_domain(source_url=source_url)
        if not domain:
            return False
        if domain in PLACEHOLDER_SOURCE_DOMAINS:
            return False
        browser_like_headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/128.0.0.0 Safari/537.36'
            )
        }

        def is_http_accessible(*, verify: bool) -> bool:
            with httpx.Client(timeout=6.0, follow_redirects=True, headers=browser_like_headers, verify=verify) as client:
                response = client.get(source_url)
            status_code = int(response.status_code)
            if status_code < 400:
                return True
            return status_code in {401, 403, 405}

        try:
            return is_http_accessible(verify=True)
        except Exception as error:
            # 目的：在 CA 鏈缺失時仍盡量判斷網址是否實際可達。
            # 為什麼：部分政府站在部署環境可能出現驗證鏈不完整，會造成誤判為不可達。
            error_text = str(error or '').upper()
            if 'CERTIFICATE_VERIFY_FAILED' not in error_text:
                return False
            try:
                return is_http_accessible(verify=False)
            except Exception:
                return False

    def _build_direct_import_overrides(self, *, model_overrides: object) -> dict:
        # 目的：建立一鍵匯入專用 LLM 覆蓋設定。
        # 為什麼：避免 Agent 自訂模型（例如 GPT-5）或 onprem 參數造成 404/路由失敗。
        base_overrides = dict(model_overrides) if isinstance(model_overrides, dict) else {}
        safe_overrides = dict(base_overrides)
        safe_overrides['tier'] = 'cloud'
        safe_overrides['provider'] = 'openai'
        safe_overrides.pop('base_url', None)
        safe_overrides.pop('onprem_base_url', None)
        safe_overrides.pop('onprem_provider', None)

        raw_model = str(safe_overrides.get('model') or '').strip()
        looks_like_unstable_model = raw_model.lower().startswith('gpt-5') or raw_model.upper().startswith('GPT-5')
        if (not raw_model) or looks_like_unstable_model:
            safe_model = str(getattr(settings, 'OPENAI_MODEL', '') or '').strip() or 'gpt-4o'
            safe_overrides['model'] = safe_model
            logger.info(
                'health_education_agent_import.override_model',
                from_model=raw_model or '(empty)',
                to_model=safe_model,
            )
        return safe_overrides

    def _normalize_import_item_for_patient_language(self, *, db: Session, normalized_item: dict) -> dict | None:
        # 目的：將候選內容整理為腎友可閱讀的語言（繁中）格式。
        # 為什麼：搜尋來源可能為英文，需先轉為繁中再進入審核流程，降低人工翻譯負擔。
        source_url = str(normalized_item.get('source_url') or '').strip()
        title = str(normalized_item.get('title') or '').strip()
        summary = str(normalized_item.get('summary') or '').strip()

        chinese_only_mode = bool(getattr(settings, 'HEALTH_EDUCATION_CHINESE_ONLY_MODE', False))
        translate_non_zh = bool(getattr(settings, 'HEALTH_EDUCATION_TRANSLATE_NON_ZH', True))
        is_chinese_source = self._is_chinese_source_url(source_url=source_url)
        has_chinese_text = self._contains_chinese_text(text=f'{title} {summary}')

        if chinese_only_mode and (not is_chinese_source) and (not has_chinese_text):
            return None
        if translate_non_zh and (not has_chinese_text):
            translated_title = self._translate_text_to_zh_tw(text=title)
            translated_summary = self._translate_text_to_zh_tw(text=summary)
            if translated_title:
                normalized_item['title'] = translated_title
            if translated_summary:
                normalized_item['summary'] = translated_summary
            tags = normalized_item.get('tags') if isinstance(normalized_item.get('tags'), list) else []
            tags.append('translated:zh-tw')
            normalized_item['tags'] = [str(tag).strip() for tag in tags if str(tag).strip()]
        return normalized_item

    def _contains_chinese_text(self, *, text: str) -> bool:
        if not text:
            return False
        return bool(re.search(r'[\u4e00-\u9fff]', str(text)))

    def _is_chinese_source_url(self, *, source_url: str) -> bool:
        parsed = urlparse(str(source_url or '').strip())
        host = str(parsed.netloc or '').strip().lower()
        path = str(parsed.path or '').strip().lower()
        if any(host.endswith(suffix) for suffix in CHINESE_DOMAIN_SUFFIXES):
            return True
        if any(path.startswith(hint) for hint in CHINESE_PATH_HINTS):
            return True
        return False

    def _translate_text_to_zh_tw(self, *, text: str) -> str:
        # 目的：將英文或其他語言文字轉為繁體中文。
        # 為什麼：腎友端閱讀語言需一致，避免英文內容造成理解門檻。
        normalized_text = str(text or '').strip()
        if not normalized_text:
            return normalized_text
        if self._contains_chinese_text(text=normalized_text):
            return normalized_text
        translator = LLMClient()
        overrides = self._build_direct_import_overrides(model_overrides={})
        try:
            translator.init_for_session(
                session_id=f'health-education-translate-{uuid.uuid4()}',
                preferred_tier='cloud',
                overrides=overrides,
            )
            translated_text = translator.complete(
                prompt=(
                    '請將下列文字翻譯成繁體中文，只輸出翻譯結果，不要加註解。\n'
                    f'{normalized_text}'
                ),
                tier='cloud',
            )
            return str(translated_text or '').strip() or normalized_text
        except Exception:
            return normalized_text

    def _trim_debug_text(self, *, raw_text: str) -> str:
        normalized_text = str(raw_text or '').strip()
        if not normalized_text:
            return '（空白）'
        if len(normalized_text) <= MAX_AGENT_OUTPUT_DEBUG_CHARS:
            return normalized_text
        return f'{normalized_text[:MAX_AGENT_OUTPUT_DEBUG_CHARS]}...(已截斷)'

    def get_source_policy(self, *, db: Session) -> dict:
        trusted_domains, blocked_domains, trusted_url_prefixes, blocked_url_prefixes = self._get_effective_source_rule_sets(db=db)
        return {
            'ok': True,
            'policy': {
                'trusted_domains': sorted(trusted_domains),
                'blocked_domains': sorted(blocked_domains),
                'trusted_url_prefixes': sorted(trusted_url_prefixes),
                'blocked_url_prefixes': sorted(blocked_url_prefixes),
                'default_policy': SOURCE_POLICY_REVIEW_REQUIRED,
                'trusted_only_mode': bool(getattr(settings, 'HEALTH_EDUCATION_TRUSTED_ONLY_MODE', False)),
            },
        }

    def list_source_rules(self, *, db: Session, enabled_only: bool = False) -> dict:
        # 目的：列出可擴充來源規則。
        # 為什麼：列表依建立時間固定排序，避免更新單筆後位置跳動影響維護操作。
        query = db.query(HealthEducationSourceRule)
        if enabled_only:
            query = query.filter(HealthEducationSourceRule.enabled == True)  # noqa: E712
        rows = query.order_by(HealthEducationSourceRule.created_at.desc()).all()
        return {
            'ok': True,
            'items': [self._serialize_source_rule(row=row) for row in rows],
        }

    def create_source_rule(self, *, db: Session, payload: dict, current_user: User | None) -> dict:
        # 目的：新增單筆來源規則。
        # 為什麼：來源白名單/黑名單需由後台快速增補，無須改環境變數重啟。
        normalized_payload = self._normalize_source_rule_payload(payload=payload)
        existing = db.query(HealthEducationSourceRule).filter(HealthEducationSourceRule.domain == normalized_payload['domain']).first()
        if existing is not None:
            raise validation_error('規則已存在，請改用編輯')
        row = HealthEducationSourceRule(
            domain=normalized_payload['domain'],
            policy=normalized_payload['policy'],
            enabled=normalized_payload['enabled'],
            note=normalized_payload['note'],
            created_by_user_id=str(getattr(current_user, 'id', '') or '') or None,
            updated_by_user_id=str(getattr(current_user, 'id', '') or '') or None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {'ok': True, 'item': self._serialize_source_rule(row=row)}

    def update_source_rule(self, *, db: Session, rule_id: str, payload: dict, current_user: User | None) -> dict:
        # 目的：更新來源規則。
        # 為什麼：營運調整政策或暫停規則時，需要保留同一筆歷史紀錄。
        row = self._get_source_rule_or_error(db=db, rule_id=rule_id)
        normalized_payload = self._normalize_source_rule_payload(payload=payload)
        duplicated = (
            db.query(HealthEducationSourceRule)
            .filter(HealthEducationSourceRule.domain == normalized_payload['domain'])
            .filter(HealthEducationSourceRule.id != row.id)
            .first()
        )
        if duplicated is not None:
            raise validation_error('規則已存在，請使用其他目標')
        row.domain = normalized_payload['domain']
        row.policy = normalized_payload['policy']
        row.enabled = normalized_payload['enabled']
        row.note = normalized_payload['note']
        row.updated_by_user_id = str(getattr(current_user, 'id', '') or '') or None
        db.add(row)
        db.commit()
        db.refresh(row)
        return {'ok': True, 'item': self._serialize_source_rule(row=row)}

    def delete_source_rule(self, *, db: Session, rule_id: str) -> dict:
        # 目的：刪除來源規則。
        # 為什麼：過時規則需可移除，避免規則集持續膨脹。
        row = self._get_source_rule_or_error(db=db, rule_id=rule_id)
        db.delete(row)
        db.commit()
        return {'ok': True, 'deleted_rule_id': str(rule_id)}

    def approve_content(self, *, db: Session, content_id: str, current_user: User | None) -> dict:
        row = self._get_content_or_error(db=db, content_id=content_id)
        row.status = 'approved'
        row.approved_by_user_id = str(getattr(current_user, 'id', '') or '') or None
        row.approved_at = datetime.now()
        db.add(row)
        db.commit()
        db.refresh(row)
        return {'ok': True, 'item': self._serialize_content(db=db, row=row)}

    def reject_content(self, *, db: Session, content_id: str, current_user: User | None) -> dict:
        row = self._get_content_or_error(db=db, content_id=content_id)
        row.status = 'rejected'
        row.approved_by_user_id = str(getattr(current_user, 'id', '') or '') or None
        row.approved_at = datetime.now()
        db.add(row)
        db.commit()
        db.refresh(row)
        return {'ok': True, 'item': self._serialize_content(db=db, row=row)}

    async def send_content_now(
        self,
        *,
        db: Session,
        content_id: str,
        audience_rule: str,
        trigger_source: str = 'manual',
    ) -> dict:
        # 目的：將核准後衛教內容推送給目標病患。
        # 為什麼：提供可追溯的立即發送能力，並與排程執行共用同一套投遞邏輯。
        row = self._get_content_or_error(db=db, content_id=content_id)
        if str(row.status or '').strip().lower() != 'approved':
            raise validation_error('內容尚未核准，無法發送')

        normalized_audience_rule = self._normalize_audience_rule(audience_rule=audience_rule)
        patient_rows = self._resolve_patients_by_audience(db=db, audience_rule=normalized_audience_rule)
        active_binding_map = self._build_active_line_binding_map(db=db)
        scheduled_count = 0
        sent_count = 0
        skipped_count = 0
        failed_count = 0
        message_text = self._build_content_message_text(content=row)

        for patient in patient_rows:
            scheduled_count += 1
            line_user_id = self._resolve_patient_line_user_id(patient=patient, active_binding_map=active_binding_map)
            if not line_user_id:
                skipped_count += 1
                self._append_delivery_log(
                    db=db,
                    content_id=str(row.id),
                    patient_id=str(patient.id),
                    line_user_id=None,
                    audience_rule=normalized_audience_rule,
                    trigger_source=trigger_source,
                    status='skipped',
                    detail='病患尚未綁定 line_user_id',
                )
                continue
            try:
                await self._push_line_message(to_line_user_id=line_user_id, text=message_text)
                self._append_delivery_log(
                    db=db,
                    content_id=str(row.id),
                    patient_id=str(patient.id),
                    line_user_id=line_user_id,
                    audience_rule=normalized_audience_rule,
                    trigger_source=trigger_source,
                    status='sent',
                    detail='衛教內容已送出',
                )
                sent_count += 1
            except Exception as error:
                logger.warning('health_education_send_failed', content_id=str(row.id), patient_id=str(patient.id), error=str(error)[:300])
                self._append_delivery_log(
                    db=db,
                    content_id=str(row.id),
                    patient_id=str(patient.id),
                    line_user_id=line_user_id,
                    audience_rule=normalized_audience_rule,
                    trigger_source=trigger_source,
                    status='failed',
                    detail=str(error)[:300],
                )
                failed_count += 1

        row.last_sent_at = datetime.now()
        if sent_count > 0:
            row.status = 'sent'
        db.add(row)
        db.commit()
        return {
            'ok': True,
            'content_id': str(row.id),
            'audience_rule': normalized_audience_rule,
            'scheduled': scheduled_count,
            'sent': sent_count,
            'skipped': skipped_count,
            'failed': failed_count,
        }

    def _resolve_patient_line_user_id(self, *, patient: RenalPatient, active_binding_map: dict[str, str]) -> str:
        # 目的：解析單一病患可用的 LINE user id。
        # 為什麼：主檔 line_user_id 與 session 綁定可能不同步，發送時需有穩定優先序避免漏送。
        active_session_line_user_id = str(active_binding_map.get(str(patient.id), '') or '').strip()
        if active_session_line_user_id:
            return active_session_line_user_id
        patient_line_user_id = str(getattr(patient, 'line_user_id', '') or '').strip()
        patient_phone_number = str(getattr(patient, 'tel_no', '') or '').strip()
        if patient_line_user_id and patient_phone_number:
            return patient_line_user_id
        return ''

    def _build_active_line_binding_map(self, *, db: Session) -> dict[str, str]:
        # 目的：建立病患對應的有效 LINE 綁定映射。
        # 為什麼：當病患主檔尚未回填 line_user_id 時，仍可用 active+bound 的 session 正確發送衛教。
        mapping: dict[str, str] = {}
        session_rows = (
            db.query(LineChannelSession)
            .filter(
                LineChannelSession.bound_patient_id.isnot(None),
                LineChannelSession.status == 'active',
                LineChannelSession.binding_status == 'bound',
            )
            .order_by(LineChannelSession.updated_at.desc())
            .all()
        )
        for session_row in session_rows:
            patient_id = str(getattr(session_row, 'bound_patient_id', '') or '').strip()
            if not patient_id or patient_id in mapping:
                continue
            line_user_id = str(getattr(session_row, 'line_user_id', '') or '').strip()
            if not line_user_id:
                continue
            mapping[patient_id] = line_user_id
        return mapping

    def schedule_content(self, *, db: Session, content_id: str, payload: dict, current_user: User | None) -> dict:
        # 目的：建立衛教內容的 Crontab 排程任務。
        # 為什麼：讓核准後內容可重用既有 Celery 排程能力，支援定時發送。
        row = self._get_content_or_error(db=db, content_id=content_id)
        if str(row.status or '').strip().lower() != 'approved':
            raise validation_error('內容尚未核准，無法建立排程')

        task_name = str((payload or {}).get('name') or '').strip() or f'衛教發送-{str(row.title or "").strip()[:20]}'
        cron_expression = str((payload or {}).get('cron_expression') or '').strip()
        timezone = str((payload or {}).get('timezone') or 'Asia/Taipei').strip() or 'Asia/Taipei'
        enabled = bool((payload or {}).get('enabled', True))
        audience_rule = self._normalize_audience_rule(audience_rule=str((payload or {}).get('audience_rule') or 'all'))
        if not cron_expression:
            raise validation_error('cron_expression 不可為空')

        template_row = self._ensure_health_scheduler_template(db=db)
        from src.services.scheduler_task_service import scheduler_task_service

        task_payload = {
            'name': task_name,
            'description': f'衛教內容排程發送：{str(row.title or "").strip()}',
            'cron_expression': cron_expression,
            'timezone': timezone,
            'template_id': str(template_row.id),
            'payload': {
                'content_id': str(row.id),
                'audience_rule': audience_rule,
            },
            'enabled': enabled,
        }
        task_response = scheduler_task_service.create_task(db=db, payload=task_payload, current_user=current_user)
        return {'ok': True, 'content': self._serialize_content(db=db, row=row), 'task': task_response.get('item')}

    def list_delivery_logs(self, *, db: Session, content_id: str, limit: int) -> dict:
        # 目的：查詢單篇內容的投遞結果。
        # 為什麼：營運需快速判斷 sent/failed/skipped 與對象明細。
        row = self._get_content_or_error(db=db, content_id=content_id)
        safe_limit = max(1, min(int(limit or 100), 500))
        log_rows = (
            db.query(HealthEducationDeliveryLog, RenalPatient)
            .join(RenalPatient, RenalPatient.id == HealthEducationDeliveryLog.patient_id)
            .filter(HealthEducationDeliveryLog.content_id == row.id)
            .order_by(HealthEducationDeliveryLog.created_at.desc())
            .limit(safe_limit)
            .all()
        )
        return {
            'ok': True,
            'content': self._serialize_content(db=db, row=row),
            'logs': [
                {
                    'id': str(log_row.id),
                    'patient_id': str(patient_row.patient_code or ''),
                    'patient_name': str(patient_row.display_name or ''),
                    'line_user_id': str(log_row.line_user_id or ''),
                    'audience_rule': str(log_row.audience_rule or ''),
                    'trigger_source': str(log_row.trigger_source or ''),
                    'status': str(log_row.status or ''),
                    'detail': str(log_row.detail or ''),
                    'sent_at': log_row.sent_at.isoformat() if log_row.sent_at else None,
                }
                for log_row, patient_row in log_rows
            ],
        }

    def delete_content(self, *, db: Session, content_id: str) -> dict:
        # 目的：刪除單筆衛教內容與其投遞紀錄。
        # 為什麼：營運需要清除失效或誤匯入內容，避免列表污染。
        row = self._get_content_or_error(db=db, content_id=content_id)
        deleted_logs = (
            db.query(HealthEducationDeliveryLog)
            .filter(HealthEducationDeliveryLog.content_id == row.id)
            .delete(synchronize_session=False)
        )
        db.delete(row)
        db.commit()
        return {
            'ok': True,
            'deleted_content_id': str(content_id),
            'deleted_logs': int(deleted_logs or 0),
        }

    def clear_rejected_contents(self, *, db: Session) -> dict:
        # 目的：批次清除 rejected 狀態的衛教內容。
        # 為什麼：審核退回資料會持續累積，需提供快速清理入口。
        rejected_rows = db.query(HealthEducationContent).filter(HealthEducationContent.status == 'rejected').all()
        if not rejected_rows:
            return {'ok': True, 'deleted_contents': 0, 'deleted_logs': 0}
        rejected_content_ids = [str(row.id) for row in rejected_rows]
        deleted_logs = (
            db.query(HealthEducationDeliveryLog)
            .filter(HealthEducationDeliveryLog.content_id.in_(rejected_content_ids))
            .delete(synchronize_session=False)
        )
        deleted_contents = (
            db.query(HealthEducationContent)
            .filter(HealthEducationContent.id.in_(rejected_content_ids))
            .delete(synchronize_session=False)
        )
        db.commit()
        return {
            'ok': True,
            'deleted_contents': int(deleted_contents or 0),
            'deleted_logs': int(deleted_logs or 0),
        }

    def _normalize_content_payload(self, *, db: Session, payload: dict) -> dict:
        # 目的：驗證建立內容請求格式。
        # 為什麼：先在服務層擋下錯誤輸入，避免髒資料進入審核流程。
        if not isinstance(payload, dict):
            raise validation_error('payload 格式錯誤')
        title = str(payload.get('title') or '').strip()
        source_url = str(payload.get('source_url') or '').strip()
        if not title:
            raise validation_error('title 不可為空')
        if not source_url:
            raise validation_error('source_url 不可為空')
        if (not source_url.startswith('https://')) and (not source_url.startswith('http://')):
            raise validation_error('source_url 需為 http/https')
        source_policy = self._resolve_source_policy(db=db, source_url=source_url)
        if source_policy == SOURCE_POLICY_BLOCKED:
            raise validation_error('來源網域被封鎖，請改用其他來源')
        tags = payload.get('tags') if isinstance(payload.get('tags'), list) else []
        normalized_tags = [str(item).strip() for item in tags if str(item).strip()]
        return {
            'title': title,
            'source_name': str(payload.get('source_name') or '').strip() or None,
            'source_url': source_url,
            'summary': str(payload.get('summary') or '').strip() or None,
            'tags': normalized_tags,
        }

    def _normalize_import_limit(self, *, raw_limit: object) -> int:
        try:
            requested_limit = int(raw_limit if raw_limit is not None else DEFAULT_IMPORT_LIMIT)
        except Exception as error:
            raise validation_error('limit 需為數字') from error
        if requested_limit <= 0:
            raise validation_error('limit 需大於 0')
        return min(requested_limit, MAX_IMPORT_LIMIT)

    def _collect_mcp_search_context(self, *, router: ChatRouter, db: Session, topic: str, limit: int) -> dict:
        # 目的：強制先呼叫 firecrawl-mcp 取得可追溯搜尋結果。
        # 為什麼：當 Agent 未觸發工具時，需先建立真實來源上下文，降低虛構網址風險。
        mcp_call_session_id = f'health-education-mcp-search-{uuid.uuid4()}'
        trusted_domains, _, _, _ = self._get_effective_source_rule_sets(db=db)
        include_domains = sorted({self._strip_www_prefix(domain=domain) for domain in trusted_domains if domain})
        payload_candidates = [
            {
                'query': topic,
                'limit': limit,
                'includeDomains': include_domains,
                'sources': [{'type': 'web'}],
            },
            {
                'query': topic,
                'limit': limit,
                'includeDomains': include_domains,
            },
            {'query': topic, 'limit': limit},
        ]
        call_attempts: list[dict] = []
        successful_result: dict | None = None

        for payload_candidate in payload_candidates:
            try:
                tool_response = router.call_mcp_tool(
                    session_id=mcp_call_session_id,
                    mcp_name=MCP_SEARCH_TOOL_NAME,
                    payload=payload_candidate,
                    db=db,
                    target_mcp_tool_name=MCP_SEARCH_TARGET_TOOL_NAME,
                )
            except Exception as error:
                call_attempts.append(
                    {
                        'payload': payload_candidate,
                        'ok': False,
                        'error': str(error)[:300],
                    }
                )
                continue

            if bool(tool_response.get('ok')):
                successful_result = tool_response
                call_attempts.append({'payload': payload_candidate, 'ok': True, 'error': ''})
                break
            call_attempts.append(
                {
                    'payload': payload_candidate,
                    'ok': False,
                    'error': str(tool_response.get('error') or '')[:300],
                }
            )

        extracted_items = self._extract_candidates_from_mcp_result(
            mcp_result=(successful_result or {}).get('result', {}),
            limit=limit,
        )
        if successful_result is not None and not extracted_items:
            logger.warning(
                'health_education_agent_import.mcp_search_empty_extract',
                source='mcp_only',
                topic=topic,
                mcp_tool_name=MCP_SEARCH_TOOL_NAME,
                mcp_result_preview=self._trim_debug_text(raw_text=json.dumps((successful_result or {}).get('result', {}), ensure_ascii=False)),
            )
        prompt_context = self._build_mcp_prompt_context(items=extracted_items)
        logger.info(
            'health_education_agent_import.mcp_search',
            source='mcp_only',
            topic=topic,
            mcp_tool_name=MCP_SEARCH_TOOL_NAME,
            call_attempts=call_attempts,
            call_ok=successful_result is not None,
            extracted_item_count=len(extracted_items),
        )
        return {
            'mcp_tool_name': MCP_SEARCH_TOOL_NAME,
            'call_ok': successful_result is not None,
            'call_attempts': call_attempts,
            'items': extracted_items,
            'prompt_context': prompt_context,
        }

    def _extract_candidates_from_mcp_result(self, *, mcp_result: object, limit: int) -> list[dict]:
        # 目的：從 MCP 搜尋結果中萃取可匯入的候選文章欄位。
        # 為什麼：MCP 回傳格式可能隨工具版本變動，需以寬鬆方式抽取 URL/標題/摘要。
        if limit <= 0:
            return []
        normalized_result = self._normalize_mcp_result_payload(mcp_result=mcp_result)
        candidate_rows = self._collect_dict_nodes(value=normalized_result)
        extracted_items: list[dict] = []
        dedupe_urls: set[str] = set()
        url_keys = ('url', 'source_url', 'link', 'href')
        title_keys = ('title', 'name', 'headline')
        summary_keys = ('summary', 'description', 'snippet', 'content')

        for row in candidate_rows:
            source_url = self._pick_first_text_value(row=row, candidate_keys=url_keys)
            if not source_url:
                continue
            normalized_url = str(source_url).strip()
            if (not normalized_url.startswith('http://')) and (not normalized_url.startswith('https://')):
                continue
            if normalized_url in dedupe_urls:
                continue
            dedupe_urls.add(normalized_url)

            title = self._pick_first_text_value(row=row, candidate_keys=title_keys) or f'衛教內容（{self._extract_domain(source_url=normalized_url)}）'
            summary = self._pick_first_text_value(row=row, candidate_keys=summary_keys) or ''
            extracted_items.append(
                {
                    'title': str(title).strip()[:200],
                    'source_name': self._extract_domain(source_url=normalized_url),
                    'source_url': normalized_url,
                    'summary': str(summary).strip()[:500],
                    'tags': ['mcp:firecrawl-mcp'],
                }
            )
            if len(extracted_items) >= limit:
                break
        return extracted_items

    def _normalize_mcp_result_payload(self, *, mcp_result: object) -> object:
        # 目的：把 MCP 工具結果轉成可遍歷的資料結構。
        # 為什麼：部分 MCP 會把 JSON 包在 `content[].text` 字串內，需先解包才能擷取網址。
        if isinstance(mcp_result, dict | list):
            candidate_payload = self._extract_structured_payload_from_mcp_content(content_payload=mcp_result)
            if candidate_payload is not None:
                return candidate_payload
            return mcp_result
        if isinstance(mcp_result, str):
            parsed = self._parse_agent_json_payload(raw_output=mcp_result)
            if parsed is not None:
                return parsed
            return {'raw_text': mcp_result}
        return {'raw_value': str(mcp_result)}

    def _extract_structured_payload_from_mcp_content(self, *, content_payload: object) -> dict | list | None:
        # 目的：從 MCP `content` 區塊抽出 JSON payload。
        # 為什麼：firecrawl_search 目前回傳 `{content:[{type:text,text:'{...json...}'}]}`，需先還原成 dict。
        content_dict = content_payload if isinstance(content_payload, dict) else {}
        content_items = content_dict.get('content') if isinstance(content_dict.get('content'), list) else []
        for content_item in content_items:
            if not isinstance(content_item, dict):
                continue
            raw_text = str(content_item.get('text') or '').strip()
            if not raw_text:
                continue
            parsed = self._parse_agent_json_payload(raw_output=raw_text)
            if isinstance(parsed, dict | list):
                return parsed
        return None

    def _collect_dict_nodes(self, *, value: object) -> list[dict]:
        # 目的：遞迴收集任意結構中的字典節點。
        # 為什麼：MCP 返回結構可能巢狀，需遍歷以穩定抓取 url/title/summary。
        nodes: list[dict] = []

        def walk(current_value: object) -> None:
            if isinstance(current_value, dict):
                nodes.append(current_value)
                for nested_value in current_value.values():
                    walk(nested_value)
                return
            if isinstance(current_value, list):
                for nested_value in current_value:
                    walk(nested_value)

        walk(value)
        return nodes

    def _pick_first_text_value(self, *, row: dict, candidate_keys: tuple[str, ...]) -> str:
        for candidate_key in candidate_keys:
            raw_value = row.get(candidate_key)
            if isinstance(raw_value, str) and raw_value.strip():
                return raw_value.strip()
        return ''

    def _build_mcp_prompt_context(self, *, items: list[dict]) -> str:
        # 目的：把 MCP 搜尋結果轉為精簡提示片段。
        # 為什麼：當直接匯入不足時，讓後續 LLM 重整仍有可追溯來源。
        if not items:
            return ''
        lines: list[str] = []
        for index, item in enumerate(items, start=1):
            title = str(item.get('title') or '（無標題）').strip()
            source_url = str(item.get('source_url') or '').strip()
            summary = str(item.get('summary') or '').strip()
            lines.append(f'{index}. title={title}; source_url={source_url}; summary={summary}')
        return '\n'.join(lines)

    def _parse_agent_json_payload(self, *, raw_output: str) -> list | dict | None:
        candidate_text = str(raw_output or '').strip()
        if not candidate_text:
            return None
        parsed_direct = self._parse_json_text(candidate_text)
        if parsed_direct is not None:
            return parsed_direct

        fenced_match = re.search(r'```json\s*([\s\S]*?)\s*```', candidate_text, flags=re.IGNORECASE)
        if fenced_match:
            parsed_fenced = self._parse_json_text(fenced_match.group(1))
            if parsed_fenced is not None:
                return parsed_fenced

        for pattern in (r'\[[\s\S]*\]', r'\{[\s\S]*\}'):
            match = re.search(pattern, candidate_text)
            if not match:
                continue
            parsed_match = self._parse_json_text(match.group(0))
            if parsed_match is not None:
                return parsed_match
        return None

    def _parse_json_text(self, candidate_text: str) -> list | dict | None:
        try:
            parsed = json.loads(str(candidate_text or '').strip())
        except Exception:
            return None
        if isinstance(parsed, list | dict):
            return parsed
        return None

    def _resolve_source_policy(self, *, db: Session, source_url: str) -> str:
        normalized_source_url = self._normalize_source_url_prefix(raw_source_url=source_url)
        domain = self._extract_domain(source_url=source_url)
        if (not domain) and (not normalized_source_url):
            return SOURCE_POLICY_REVIEW_REQUIRED
        trusted_domains, blocked_domains, trusted_url_prefixes, blocked_url_prefixes = self._get_effective_source_rule_sets(db=db)
        if normalized_source_url and self._url_matches_prefixes(source_url=normalized_source_url, url_prefixes=blocked_url_prefixes):
            return SOURCE_POLICY_BLOCKED
        if normalized_source_url and self._url_matches_prefixes(source_url=normalized_source_url, url_prefixes=trusted_url_prefixes):
            return SOURCE_POLICY_TRUSTED
        if self._domain_matches(domain=domain, domain_set=blocked_domains):
            return SOURCE_POLICY_BLOCKED
        if self._domain_matches(domain=domain, domain_set=trusted_domains):
            return SOURCE_POLICY_TRUSTED
        return SOURCE_POLICY_REVIEW_REQUIRED

    def _get_effective_source_rule_sets(self, *, db: Session) -> tuple[set[str], set[str], set[str], set[str]]:
        # 目的：彙整來源規則（網域與網址前綴）供策略判斷使用。
        # 為什麼：同網域多語系網站常以路徑區分語言，僅靠網域規則不足以精準治理來源。
        trusted_domains = set(self._parse_domain_list(getattr(settings, 'HEALTH_EDUCATION_TRUSTED_DOMAINS', '')))
        blocked_domains = set(self._parse_domain_list(getattr(settings, 'HEALTH_EDUCATION_BLOCKED_DOMAINS', '')))
        trusted_url_prefixes: set[str] = set()
        blocked_url_prefixes: set[str] = set()
        rule_rows = (
            db.query(HealthEducationSourceRule)
            .filter(HealthEducationSourceRule.enabled == True)  # noqa: E712
            .all()
        )
        for rule_row in rule_rows:
            raw_target = str(getattr(rule_row, 'domain', '') or '').strip()
            policy = str(getattr(rule_row, 'policy', '') or '').strip().lower()
            if not raw_target:
                continue
            normalized_target = self._normalize_source_rule_target(raw_target=raw_target)
            if not normalized_target:
                continue
            is_url_prefix_target = normalized_target.startswith('http://') or normalized_target.startswith('https://')
            if policy == SOURCE_POLICY_BLOCKED:
                if is_url_prefix_target:
                    blocked_url_prefixes.add(normalized_target)
                    trusted_url_prefixes.discard(normalized_target)
                else:
                    blocked_domains.add(normalized_target)
                    trusted_domains.discard(normalized_target)
                continue
            if policy == SOURCE_POLICY_TRUSTED:
                if is_url_prefix_target:
                    trusted_url_prefixes.add(normalized_target)
                    blocked_url_prefixes.discard(normalized_target)
                else:
                    trusted_domains.add(normalized_target)
                    blocked_domains.discard(normalized_target)
        return trusted_domains, blocked_domains, trusted_url_prefixes, blocked_url_prefixes

    def _normalize_source_rule_payload(self, *, payload: dict) -> dict:
        # 目的：驗證來源規則輸入資料。
        # 為什麼：同時支援網域與網址前綴規則，讓多語系網站可做細粒度治理。
        if not isinstance(payload, dict):
            raise validation_error('payload 格式錯誤')
        raw_target = str(payload.get('domain') or '').strip()
        normalized_target = self._normalize_source_rule_target(raw_target=raw_target)
        if not normalized_target:
            raise validation_error('規則目標不可為空')
        policy = str(payload.get('policy') or SOURCE_POLICY_TRUSTED).strip().lower()
        if policy not in {SOURCE_POLICY_TRUSTED, SOURCE_POLICY_BLOCKED}:
            raise validation_error('policy 僅支援 trusted/blocked')
        return {
            'domain': normalized_target,
            'policy': policy,
            'enabled': bool(payload.get('enabled', True)),
            'note': str(payload.get('note') or '').strip() or None,
        }

    def _normalize_source_rule_target(self, *, raw_target: str) -> str:
        # 目的：正規化來源規則目標（網域或網址前綴）。
        # 為什麼：讓維護頁可直接貼網址（含路徑），不會被強制退化成僅網域。
        normalized_text = str(raw_target or '').strip()
        if not normalized_text:
            return ''
        if normalized_text.startswith('http://') or normalized_text.startswith('https://'):
            return self._normalize_source_url_prefix(raw_source_url=normalized_text)
        if '/' in normalized_text:
            # 目的：支援未帶 scheme 的網址前綴輸入。
            # 為什麼：營運常直接貼 `domain/path`，若只當網域處理會意外丟失路徑資訊。
            return self._normalize_source_url_prefix(raw_source_url=f'https://{normalized_text.lstrip("/")}')
        return self._normalize_domain(normalized_text)

    def _normalize_domain(self, raw_domain: str) -> str:
        domain = str(raw_domain or '').strip().lower()
        if not domain:
            return ''
        if domain.startswith('http://') or domain.startswith('https://'):
            parsed = urlparse(domain)
            domain = str(parsed.netloc or '').strip().lower()
        if '/' in domain:
            domain = domain.split('/')[0].strip()
        if ':' in domain:
            domain = domain.split(':')[0].strip()
        return domain.lstrip('.').rstrip('.')

    def _normalize_source_url_prefix(self, *, raw_source_url: str) -> str:
        # 目的：把來源網址正規化為可比較的前綴字串。
        # 為什麼：不同輸入可能帶 query/hash，若不清理會讓規則命中不穩定。
        raw_text = str(raw_source_url or '').strip()
        if not raw_text:
            return ''
        parsed = urlparse(raw_text)
        scheme = str(parsed.scheme or '').strip().lower()
        netloc = str(parsed.netloc or '').strip().lower()
        if scheme not in {'http', 'https'} or (not netloc):
            return ''
        path = str(parsed.path or '').strip()
        if path and (not path.startswith('/')):
            path = f'/{path}'
        if path and path != '/':
            path = path.rstrip('/')
        return f'{scheme}://{netloc}{path}'

    def _serialize_source_rule(self, *, row: HealthEducationSourceRule) -> dict:
        return {
            'id': str(row.id),
            'domain': str(row.domain or ''),
            'policy': str(row.policy or ''),
            'enabled': bool(row.enabled),
            'note': str(row.note or ''),
            'created_by_user_id': str(row.created_by_user_id) if row.created_by_user_id else None,
            'updated_by_user_id': str(row.updated_by_user_id) if row.updated_by_user_id else None,
            'created_at': row.created_at.isoformat() if row.created_at else None,
            'updated_at': row.updated_at.isoformat() if row.updated_at else None,
        }

    def _get_source_rule_or_error(self, *, db: Session, rule_id: str) -> HealthEducationSourceRule:
        row = db.query(HealthEducationSourceRule).filter(HealthEducationSourceRule.id == rule_id).first()
        if row is None:
            raise not_found_error('HealthEducationSourceRule', rule_id)
        return row

    def _extract_domain(self, *, source_url: str) -> str:
        parsed = urlparse(str(source_url or '').strip())
        return str(parsed.netloc or '').strip().lower()

    def _parse_domain_list(self, raw_value: str) -> list[str]:
        return [str(item or '').strip().lower() for item in str(raw_value or '').split(',') if str(item or '').strip()]

    def _url_matches_prefixes(self, *, source_url: str, url_prefixes: set[str]) -> bool:
        normalized_source_url = self._normalize_source_url_prefix(raw_source_url=source_url)
        if not normalized_source_url:
            return False
        for raw_prefix in url_prefixes:
            normalized_prefix = self._normalize_source_url_prefix(raw_source_url=raw_prefix)
            if not normalized_prefix:
                continue
            if normalized_source_url == normalized_prefix:
                return True
            if normalized_source_url.startswith(f'{normalized_prefix}/'):
                return True
        return False

    def _domain_matches(self, *, domain: str, domain_set: set[str]) -> bool:
        # 目的：用寬鬆且可預測的規則比對網域（含子網域與 www 變形）。
        # 為什麼：營運常混用 `www.example.com` 與 `example.com`，若只做字串完全比對會造成規則失效。
        normalized_domain = self._strip_www_prefix(domain=domain)
        for candidate in domain_set:
            normalized_candidate = self._strip_www_prefix(domain=candidate)
            if normalized_domain == normalized_candidate:
                return True
            if normalized_domain.endswith(f'.{normalized_candidate}'):
                return True
        return False

    def _strip_www_prefix(self, *, domain: str) -> str:
        normalized_domain = str(domain or '').strip().lower()
        if normalized_domain.startswith('www.'):
            return normalized_domain[4:]
        return normalized_domain

    def _normalize_audience_rule(self, *, audience_rule: str) -> str:
        normalized_audience_rule = str(audience_rule or 'all').strip().lower() or 'all'
        if normalized_audience_rule not in ALLOWED_AUDIENCE_RULES:
            raise validation_error('audience_rule 僅支援 all/diabetic_only/non_diabetic_only')
        return normalized_audience_rule

    def _resolve_patients_by_audience(self, *, db: Session, audience_rule: str) -> list[RenalPatient]:
        query = db.query(RenalPatient).filter(RenalPatient.enabled == True)  # noqa: E712
        if audience_rule == 'diabetic_only':
            query = query.filter(RenalPatient.is_diabetic == True)  # noqa: E712
        elif audience_rule == 'non_diabetic_only':
            query = query.filter(RenalPatient.is_diabetic == False)  # noqa: E712
        return query.order_by(RenalPatient.patient_code.asc()).all()

    def _build_content_message_text(self, *, content: HealthEducationContent) -> str:
        title = str(content.title or '').strip()
        summary = str(content.summary or '').strip()
        source_url = str(content.source_url or '').strip()
        if summary:
            return f'衛教分享｜{title}\n{summary}\n{source_url}'
        return f'衛教分享｜{title}\n{source_url}'

    def _append_delivery_log(
        self,
        *,
        db: Session,
        content_id: str,
        patient_id: str,
        line_user_id: str | None,
        audience_rule: str,
        trigger_source: str,
        status: str,
        detail: str,
    ) -> None:
        db.add(
            HealthEducationDeliveryLog(
                content_id=content_id,
                patient_id=patient_id,
                line_user_id=str(line_user_id or '').strip() or None,
                audience_rule=audience_rule,
                trigger_source=trigger_source,
                status=status,
                detail=str(detail or '').strip() or None,
                payload={},
                sent_at=datetime.now(),
            )
        )

    async def _push_line_message(self, *, to_line_user_id: str, text: str) -> None:
        channel_access_token = str(getattr(settings, 'LINE_CHANNEL_ACCESS_TOKEN', '') or '').strip()
        if not channel_access_token:
            raise validation_error('LINE_CHANNEL_ACCESS_TOKEN 尚未設定')
        payload = {
            'to': str(to_line_user_id or '').strip(),
            'messages': [
                {
                    'type': 'text',
                    'text': str(text or '').strip()[:5000],
                }
            ],
        }
        headers = {
            'Authorization': f'Bearer {channel_access_token}',
            'Content-Type': 'application/json',
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post('https://api.line.me/v2/bot/message/push', headers=headers, json=payload)
        if response.status_code >= 400:
            raise validation_error(f'LINE push 失敗: {response.status_code} {response.text[:200]}')

    async def send_next_content_now(self, *, db: Session, audience_rule: str, trigger_source: str, cooldown_days: int) -> dict:
        # 目的：自動挑選符合冷卻期的核准衛教內容並立即發送。
        # 為什麼：固定排程需要每次發送不同文章，避免在短期內重複推播造成訊息疲乏。
        normalized_audience_rule = self._normalize_audience_rule(audience_rule=audience_rule)
        safe_cooldown_days = self._normalize_cooldown_days(cooldown_days=cooldown_days)
        selected_content = self._pick_next_dispatchable_content(db=db, cooldown_days=safe_cooldown_days)
        if selected_content is None:
            return {
                'ok': True,
                'dispatch_mode': 'rotate',
                'cooldown_days': safe_cooldown_days,
                'selected_content_id': None,
                'selected_title': '',
                'scheduled': 0,
                'sent': 0,
                'skipped': 0,
                'failed': 0,
                'reason': 'no_eligible_content',
            }
        send_result = await self.send_content_now(
            db=db,
            content_id=str(selected_content.id),
            audience_rule=normalized_audience_rule,
            trigger_source=trigger_source,
        )
        send_result['dispatch_mode'] = 'rotate'
        send_result['cooldown_days'] = safe_cooldown_days
        send_result['selected_content_id'] = str(selected_content.id)
        send_result['selected_title'] = str(selected_content.title or '')
        return send_result

    def _ensure_health_scheduler_template(self, *, db: Session) -> ScheduledTaskTemplate:
        # 目的：確保衛教排程模板存在且規格一致。
        # 為什麼：schedule API 需可直接建立任務，不應要求使用者手動先建模板。
        row = db.query(ScheduledTaskTemplate).filter(ScheduledTaskTemplate.template_key == DEFAULT_SCHEDULER_TEMPLATE_KEY).first()
        if row is not None:
            row.enabled = True
            row.executor_type = 'health_education_dispatch'
            row.payload_schema = {}
            row.default_payload = {'audience_rule': 'all', 'dispatch_mode': 'fixed', 'cooldown_days': DEFAULT_DISPATCH_COOLDOWN_DAYS}
            db.add(row)
            db.commit()
            db.refresh(row)
            return row
        row = ScheduledTaskTemplate(
            template_key=DEFAULT_SCHEDULER_TEMPLATE_KEY,
            name='衛教內容排程發送',
            description='定時發送核准後衛教內容',
            executor_type='health_education_dispatch',
            payload_schema={},
            default_payload={'audience_rule': 'all', 'dispatch_mode': 'fixed', 'cooldown_days': DEFAULT_DISPATCH_COOLDOWN_DAYS},
            enabled=True,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def _normalize_cooldown_days(self, *, cooldown_days: int) -> int:
        # 目的：把冷卻天數限制在可預期範圍。
        # 為什麼：避免 0 天導致重複推播，或過大天數造成長期無可發送內容。
        return max(1, min(int(cooldown_days or DEFAULT_DISPATCH_COOLDOWN_DAYS), MAX_DISPATCH_COOLDOWN_DAYS))

    def _pick_next_dispatchable_content(self, *, db: Session, cooldown_days: int) -> HealthEducationContent | None:
        # 目的：依最後成功發送時間挑選下一篇可推播衛教。
        # 為什麼：要實作固定排程輪替，需用「已送出紀錄」避免一個月內重複文章。
        cooldown_threshold = datetime.now() - timedelta(days=cooldown_days)
        sent_log_subquery = (
            db.query(
                HealthEducationDeliveryLog.content_id.label('content_id'),
                func.max(HealthEducationDeliveryLog.sent_at).label('last_sent_at'),
            )
            .filter(HealthEducationDeliveryLog.status == 'sent')
            .group_by(HealthEducationDeliveryLog.content_id)
            .subquery()
        )
        candidate_rows = (
            db.query(HealthEducationContent, sent_log_subquery.c.last_sent_at)
            .outerjoin(sent_log_subquery, sent_log_subquery.c.content_id == HealthEducationContent.id)
            .filter(HealthEducationContent.status == 'approved')
            .order_by(sent_log_subquery.c.last_sent_at.is_(None).desc(), sent_log_subquery.c.last_sent_at.asc(), HealthEducationContent.updated_at.asc())
            .all()
        )
        for content_row, last_sent_at in candidate_rows:
            if last_sent_at is not None and last_sent_at > cooldown_threshold:
                continue
            return content_row
        return None

    def _get_content_or_error(self, *, db: Session, content_id: str) -> HealthEducationContent:
        row = db.query(HealthEducationContent).filter(HealthEducationContent.id == content_id).first()
        if row is None:
            raise not_found_error('HealthEducationContent', content_id)
        return row

    def _serialize_content(self, *, db: Session, row: HealthEducationContent) -> dict:
        return {
            'id': str(row.id),
            'title': str(row.title or ''),
            'source_name': str(row.source_name or ''),
            'source_url': str(row.source_url or ''),
            'summary': str(row.summary or ''),
            'tags': row.tags if isinstance(row.tags, list) else [],
            'status': str(row.status or ''),
            'source_policy': self._resolve_source_policy(db=db, source_url=str(row.source_url or '')),
            'approved_by_user_id': str(row.approved_by_user_id) if row.approved_by_user_id else None,
            'approved_at': row.approved_at.isoformat() if row.approved_at else None,
            'last_sent_at': row.last_sent_at.isoformat() if row.last_sent_at else None,
            'updated_at': row.updated_at.isoformat() if row.updated_at else None,
            'created_at': row.created_at.isoformat() if row.created_at else None,
        }


health_education_service = HealthEducationService()
