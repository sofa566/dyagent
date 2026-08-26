from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import uuid

from sqlalchemy.orm import Session

from src.core.config import settings
from src.models import FunctionProfile, MCPConnection, SkillEntry, ToolExecutionAudit


SUPPORTED_RISK_LEVELS = {'safe', 'restricted', 'dangerous'}
SUPPORTED_COST_CLASSES = {'free', 'billable'}
DEFAULT_ALLOW_SCOPES = ['master', 'public', 'tasked', 'private']


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
        confirmation_passed = bool((payload or {}).get('_policy_confirmed') or (payload or {}).get('_confirmed'))
        if confirmation_required and not confirmation_passed:
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
        daily_limit = self._parse_positive_int(rate_limit_profile.get('per_user_daily_calls'))
        monthly_limit = self._parse_positive_int(rate_limit_profile.get('per_user_monthly_calls'))
        if daily_limit is None and monthly_limit is None:
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

        return True, 'pass'

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
