from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.config import settings
from src.models import (
    AccessGroup,
    FunctionProfile,
    MCPConnection,
    SkillEntry,
    ToolExecutionAudit,
    ToolExecutionConfirmation,
    UserGroupBinding,
)

SUPPORTED_RISK_LEVELS = {'safe', 'restricted', 'dangerous'}
SUPPORTED_COST_CLASSES = {'free', 'billable'}
DEFAULT_ALLOW_SCOPES = ['master', 'public', 'tasked', 'private']
POLICY_CONFIRM_META_KEYS = {'_policy_confirmed', '_confirmed', '_policy_confirm_token'}


@dataclass
class ToolPolicyDecision:
    # 目的：封裝單次工具呼叫的策略判斷結果。
    # 為什麼：統一同步/非同步路徑判斷輸出，降低分支重複。
    passed: bool
    reason: str
    status: str
    tool_type: str
    risk_level: str
    cost_class: str
    requires_confirmation: bool
    confirmation_passed: bool
    quota_passed: bool
    policy: dict
    confirmation_token: str | None = None
    confirmation_token_expires_at: datetime | None = None


class ToolPolicyService:
    # 目的：集中處理工具風險策略解析、執行判斷與稽核紀錄。
    # 為什麼：工具執行治理為跨路由邏輯，需避免散落於 API 層。

    def normalize_policy(self, raw_policy: dict | None) -> dict:
        source_policy = raw_policy if isinstance(raw_policy, dict) else {}
        risk_level = str(source_policy.get('risk_level') or 'safe').strip().lower()
        if risk_level not in SUPPORTED_RISK_LEVELS:
            risk_level = 'safe'

        cost_class = str(source_policy.get('cost_class') or 'free').strip().lower()
        if cost_class not in SUPPORTED_COST_CLASSES:
            cost_class = 'free'

        allow_scopes_raw = source_policy.get('allow_scopes')
        allow_scopes = [
            str(scope_item).strip()
            for scope_item in list(allow_scopes_raw or [])
            if str(scope_item).strip()
        ]
        if not allow_scopes:
            allow_scopes = list(DEFAULT_ALLOW_SCOPES)

        rate_limit_profile = source_policy.get('rate_limit_profile')
        if not isinstance(rate_limit_profile, dict):
            rate_limit_profile = {}

        explicit_confirmation = source_policy.get('requires_confirmation')
        requires_confirmation = bool(explicit_confirmation) if explicit_confirmation is not None else False
        if risk_level == 'dangerous' and bool(getattr(settings, 'TOOL_EXEC_REQUIRE_CONFIRM_FOR_DANGEROUS', True)):
            requires_confirmation = True

        return {
            'risk_level': risk_level,
            'cost_class': cost_class,
            'allow_scopes': allow_scopes,
            'rate_limit_profile': rate_limit_profile,
            'requires_confirmation': requires_confirmation,
        }

    def resolve_tool_policy(self, *, db: Session, tool_name: str) -> tuple[str, dict]:
        normalized_tool_name = str(tool_name or '').strip()
        if normalized_tool_name.startswith('mcp:'):
            connection_name = normalized_tool_name.split(':', 1)[1]
            row = db.query(MCPConnection).filter(MCPConnection.name == connection_name).first()
            return 'mcp', self.normalize_policy(getattr(row, 'execution_policy', {}) if row is not None else {})

        skill_row = db.query(SkillEntry).filter(SkillEntry.name == normalized_tool_name).first()
        if skill_row is not None:
            return 'skill', self.normalize_policy(getattr(skill_row, 'execution_policy', {}))

        function_row = db.query(FunctionProfile).filter(FunctionProfile.name == normalized_tool_name).first()
        if function_row is not None:
            return 'function', self.normalize_policy(getattr(function_row, 'execution_policy', {}))

        return 'unknown', self.normalize_policy({})

    def evaluate_execution(
        self,
        *,
        db: Session,
        tool_name: str,
        payload: dict,
        agent_class: str,
        user_id: str,
        agent_id: str | None = None,
        conversation_id: str | None = None,
    ) -> ToolPolicyDecision:
        tool_type, policy = self.resolve_tool_policy(db=db, tool_name=tool_name)

        normalized_agent_class = str(agent_class or '').strip()
        allow_scopes = [str(scope_item).strip() for scope_item in list(policy.get('allow_scopes') or []) if str(scope_item).strip()]
        if allow_scopes and normalized_agent_class and normalized_agent_class not in allow_scopes:
            return ToolPolicyDecision(
                passed=False,
                reason='scope_denied',
                status='denied',
                tool_type=tool_type,
                risk_level=str(policy.get('risk_level') or 'safe'),
                cost_class=str(policy.get('cost_class') or 'free'),
                requires_confirmation=bool(policy.get('requires_confirmation')),
                confirmation_passed=True,
                quota_passed=True,
                policy=policy,
            )

        confirmation_required = bool(policy.get('requires_confirmation'))
        confirmation_token = str((payload or {}).get('_policy_confirm_token') or '').strip()
        confirmation_passed = True
        issued_confirmation_token = None
        issued_confirmation_token_expires_at = None
        if confirmation_required:
            confirmation_passed = self._consume_confirmation_token(
                db=db,
                token=confirmation_token,
                user_id=user_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                tool_name=tool_name,
                payload=payload or {},
            )
        if confirmation_required and not confirmation_passed:
            issued_confirmation_token, issued_confirmation_token_expires_at = self._issue_confirmation_token(
                db=db,
                user_id=user_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                tool_name=tool_name,
                payload=payload or {},
            )
            return ToolPolicyDecision(
                passed=False,
                reason='confirmation_required',
                status='pending_confirmation',
                tool_type=tool_type,
                risk_level=str(policy.get('risk_level') or 'safe'),
                cost_class=str(policy.get('cost_class') or 'free'),
                requires_confirmation=confirmation_required,
                confirmation_passed=False,
                quota_passed=True,
                policy=policy,
                confirmation_token=issued_confirmation_token,
                confirmation_token_expires_at=issued_confirmation_token_expires_at,
            )

        quota_passed, quota_reason = self._check_quota(
            db=db,
            user_id=user_id,
            tool_name=tool_name,
            rate_limit_profile=policy.get('rate_limit_profile') if isinstance(policy.get('rate_limit_profile'), dict) else {},
        )
        if not quota_passed:
            return ToolPolicyDecision(
                passed=False,
                reason=quota_reason,
                status='denied',
                tool_type=tool_type,
                risk_level=str(policy.get('risk_level') or 'safe'),
                cost_class=str(policy.get('cost_class') or 'free'),
                requires_confirmation=confirmation_required,
                confirmation_passed=confirmation_passed,
                quota_passed=False,
                policy=policy,
            )

        return ToolPolicyDecision(
            passed=True,
            reason='pass',
            status='allowed',
            tool_type=tool_type,
            risk_level=str(policy.get('risk_level') or 'safe'),
            cost_class=str(policy.get('cost_class') or 'free'),
            requires_confirmation=confirmation_required,
            confirmation_passed=confirmation_passed or not confirmation_required,
            quota_passed=True,
            policy=policy,
        )

    def _issue_confirmation_token(
        self,
        *,
        db: Session,
        user_id: str,
        agent_id: str | None,
        conversation_id: str | None,
        tool_name: str,
        payload: dict,
    ) -> tuple[str | None, datetime | None]:
        parsed_user_id = self._parse_uuid_or_none(user_id)
        if parsed_user_id is None:
            return None, None
        raw_token = secrets.token_urlsafe(32)
        now = datetime.now()
        ttl_seconds = max(30, int(getattr(settings, 'TOOL_CONFIRM_TOKEN_TTL_SECONDS', 300) or 300))
        expires_at = now + timedelta(seconds=ttl_seconds)
        token_hash = self._hash_token(raw_token)
        payload_hash = self._build_payload_hash(payload)
        row = ToolExecutionConfirmation(
            token_hash=token_hash,
            user_id=parsed_user_id,
            agent_id=self._parse_uuid_or_none(agent_id),
            conversation_id=self._parse_uuid_or_none(conversation_id),
            tool_name=str(tool_name or '').strip() or '<empty>',
            payload_hash=payload_hash,
            expires_at=expires_at,
            used_at=None,
            created_at=now,
        )
        try:
            db.add(row)
            db.commit()
            return raw_token, expires_at
        except Exception:
            db.rollback()
            return None, None

    def _consume_confirmation_token(
        self,
        *,
        db: Session,
        token: str,
        user_id: str,
        agent_id: str | None,
        conversation_id: str | None,
        tool_name: str,
        payload: dict,
    ) -> bool:
        normalized_token = str(token or '').strip()
        if not normalized_token:
            return False

        user_uuid = self._parse_uuid_or_none(user_id)
        if user_uuid is None:
            return False

        token_hash = self._hash_token(normalized_token)
        row = db.query(ToolExecutionConfirmation).filter(ToolExecutionConfirmation.token_hash == token_hash).first()
        if row is None:
            return False

        now = datetime.now()
        expected_payload_hash = self._build_payload_hash(payload)
        same_user = row.user_id == user_uuid
        same_tool = str(row.tool_name or '').strip() == (str(tool_name or '').strip() or '<empty>')
        same_payload = str(row.payload_hash or '') == expected_payload_hash
        not_used = row.used_at is None
        not_expired = isinstance(row.expires_at, datetime) and row.expires_at > now

        expected_agent = self._parse_uuid_or_none(agent_id)
        expected_conversation = self._parse_uuid_or_none(conversation_id)
        same_agent = (row.agent_id is None) or (expected_agent is not None and row.agent_id == expected_agent)
        same_conversation = (row.conversation_id is None) or (expected_conversation is not None and row.conversation_id == expected_conversation)

        if not (same_user and same_tool and same_payload and not_used and not_expired and same_agent and same_conversation):
            return False

        try:
            row.used_at = now
            db.add(row)
            db.commit()
            return True
        except Exception:
            db.rollback()
            return False

    def _build_payload_hash(self, payload: dict) -> str:
        normalized_payload = payload if isinstance(payload, dict) else {}
        filtered_payload = {
            str(key): value
            for key, value in normalized_payload.items()
            if str(key) not in POLICY_CONFIRM_META_KEYS
        }
        encoded_payload = json.dumps(filtered_payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
        return hashlib.sha256(encoded_payload.encode('utf-8')).hexdigest()

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()

    def record_audit(
        self,
        *,
        db: Session,
        decision: ToolPolicyDecision,
        user_id: str | None,
        agent_id: str | None,
        conversation_id: str | None,
        tool_name: str,
        status: str,
        payload_keys: list[str],
        deny_reason: str | None = None,
        cost_estimate: Decimal | None = None,
        latency_ms: int | None = None,
        details: dict | None = None,
    ) -> None:
        try:
            audit_row = ToolExecutionAudit(
                user_id=self._parse_uuid_or_none(user_id),
                agent_id=self._parse_uuid_or_none(agent_id),
                conversation_id=self._parse_uuid_or_none(conversation_id),
                tool_name=str(tool_name or '').strip() or '<empty>',
                tool_type=str(decision.tool_type or 'unknown'),
                risk_level=str(decision.risk_level or 'safe'),
                cost_class=str(decision.cost_class or 'free'),
                allowlist_passed=True,
                confirmation_required=bool(decision.requires_confirmation),
                confirmation_passed=bool(decision.confirmation_passed),
                quota_passed=bool(decision.quota_passed),
                status=str(status or decision.status),
                deny_reason=str(deny_reason or '') or None,
                payload_keys=list(dict.fromkeys(payload_keys or [])),
                cost_estimate=cost_estimate,
                latency_ms=latency_ms,
                details=details if isinstance(details, dict) else {},
            )
            db.add(audit_row)
            db.commit()
        except Exception:
            db.rollback()

    def _check_quota(self, *, db: Session, user_id: str, tool_name: str, rate_limit_profile: dict) -> tuple[bool, str]:
        # 目的：檢查工具執行是否超過個人/群組次數與月成本配額。
        # 為什麼：將配額治理集中在單一函式，避免策略判斷路徑分散且難以維護。
        daily_limit = self._parse_positive_int(rate_limit_profile.get('per_user_daily_calls'))
        monthly_limit = self._parse_positive_int(rate_limit_profile.get('per_user_monthly_calls'))
        group_daily_limit = self._parse_positive_int(rate_limit_profile.get('per_group_daily_calls'))
        group_monthly_limit = self._parse_positive_int(rate_limit_profile.get('per_group_monthly_calls'))
        monthly_cost_limit = self._parse_positive_decimal(rate_limit_profile.get('monthly_cost_usd'))
        if (
            daily_limit is None
            and monthly_limit is None
            and group_daily_limit is None
            and group_monthly_limit is None
            and monthly_cost_limit is None
        ):
            return True, 'pass'

        user_uuid = self._parse_uuid_or_none(user_id)
        if user_uuid is None:
            return False, 'quota_user_invalid'

        now = datetime.now()
        if daily_limit is not None:
            day_start = datetime(now.year, now.month, now.day)
            day_end = day_start + timedelta(days=1)
            day_count = db.query(ToolExecutionAudit).filter(
                ToolExecutionAudit.user_id == user_uuid,
                ToolExecutionAudit.tool_name == tool_name,
                ToolExecutionAudit.status == 'success',
                ToolExecutionAudit.created_at >= day_start,
                ToolExecutionAudit.created_at < day_end,
            ).count()
            if day_count >= daily_limit:
                return False, 'quota_daily_calls_exceeded'

        if monthly_limit is not None:
            month_start = datetime(now.year, now.month, 1)
            next_month = datetime(now.year + 1, 1, 1) if now.month == 12 else datetime(now.year, now.month + 1, 1)
            month_count = db.query(ToolExecutionAudit).filter(
                ToolExecutionAudit.user_id == user_uuid,
                ToolExecutionAudit.tool_name == tool_name,
                ToolExecutionAudit.status == 'success',
                ToolExecutionAudit.created_at >= month_start,
                ToolExecutionAudit.created_at < next_month,
            ).count()
            if month_count >= monthly_limit:
                return False, 'quota_monthly_calls_exceeded'

        if group_daily_limit is not None or group_monthly_limit is not None:
            current_user_group_ids = self._load_user_group_ids(db=db, user_uuid=user_uuid)
            if current_user_group_ids:
                if group_daily_limit is not None:
                    day_start = datetime(now.year, now.month, now.day)
                    day_end = day_start + timedelta(days=1)
                    for group_id in current_user_group_ids:
                        group_day_count = db.query(ToolExecutionAudit.id).join(
                            UserGroupBinding,
                            UserGroupBinding.user_id == ToolExecutionAudit.user_id,
                        ).filter(
                            UserGroupBinding.group_id == group_id,
                            ToolExecutionAudit.tool_name == tool_name,
                            ToolExecutionAudit.status == 'success',
                            ToolExecutionAudit.created_at >= day_start,
                            ToolExecutionAudit.created_at < day_end,
                        ).count()
                        if group_day_count >= group_daily_limit:
                            return False, 'quota_group_daily_calls_exceeded'

                if group_monthly_limit is not None:
                    month_start = datetime(now.year, now.month, 1)
                    next_month = datetime(now.year + 1, 1, 1) if now.month == 12 else datetime(now.year, now.month + 1, 1)
                    for group_id in current_user_group_ids:
                        group_month_count = db.query(ToolExecutionAudit.id).join(
                            UserGroupBinding,
                            UserGroupBinding.user_id == ToolExecutionAudit.user_id,
                        ).filter(
                            UserGroupBinding.group_id == group_id,
                            ToolExecutionAudit.tool_name == tool_name,
                            ToolExecutionAudit.status == 'success',
                            ToolExecutionAudit.created_at >= month_start,
                            ToolExecutionAudit.created_at < next_month,
                        ).count()
                        if group_month_count >= group_monthly_limit:
                            return False, 'quota_group_monthly_calls_exceeded'

        if monthly_cost_limit is not None:
            month_start = datetime(now.year, now.month, 1)
            next_month = datetime(now.year + 1, 1, 1) if now.month == 12 else datetime(now.year, now.month + 1, 1)
            monthly_cost_total = db.query(
                func.coalesce(func.sum(ToolExecutionAudit.cost_estimate), 0)
            ).filter(
                ToolExecutionAudit.user_id == user_uuid,
                ToolExecutionAudit.tool_name == tool_name,
                ToolExecutionAudit.status == 'success',
                ToolExecutionAudit.created_at >= month_start,
                ToolExecutionAudit.created_at < next_month,
            ).scalar()
            normalized_monthly_cost_total = Decimal(str(monthly_cost_total or 0))
            if normalized_monthly_cost_total >= monthly_cost_limit:
                return False, 'quota_monthly_cost_exceeded'

        return True, 'pass'

    def _load_user_group_ids(self, *, db: Session, user_uuid: uuid.UUID) -> list[uuid.UUID]:
        # 目的：載入使用者所屬且啟用中的群組 ID。
        # 為什麼：群組配額需基於群組成員共同使用量計算，避免只看個人維度。
        group_id_rows = db.query(UserGroupBinding.group_id).join(
            AccessGroup,
            AccessGroup.id == UserGroupBinding.group_id,
        ).filter(
            UserGroupBinding.user_id == user_uuid,
            AccessGroup.enabled == True,  # noqa: E712
        ).all()
        return [group_id_row.group_id for group_id_row in group_id_rows]

    def _parse_positive_decimal(self, raw_value: object) -> Decimal | None:
        try:
            parsed = Decimal(str(raw_value))
        except Exception:
            return None
        if parsed <= 0:
            return None
        return parsed

    def _parse_positive_int(self, raw_value: object) -> int | None:
        try:
            parsed = int(raw_value)  # type: ignore[arg-type]
        except Exception:
            return None
        if parsed <= 0:
            return None
        return parsed

    def _parse_uuid_or_none(self, raw_value: str | None):
        text = str(raw_value or '').strip()
        if not text:
            return None
        try:
            return uuid.UUID(text)
        except Exception:
            return None
