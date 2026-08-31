from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Integer, and_, case, cast, func
from sqlalchemy.orm import Session

from src.models import (
    AccessGroup,
    Agent,
    Conversation,
    LlmCostAlertEvent,
    LlmCostPolicy,
    LlmTurn,
    User,
    UserGroupBinding,
)


@dataclass(frozen=True)
class CostWindow:
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class CostGuardDecision:
    allowed: bool
    reason: str
    scope_type: str | None
    scope_id: str | None
    metric_key: str | None
    current_value: float
    limit_value: float
    usage_percent: float


class CostUsageService:
    # 目的：集中處理 LLM token/cost 四層聚合查詢。
    # 為什麼：避免成本口徑散落於多個路由，降低治理規則漂移風險。

    def build_window(self, *, start_at: datetime, end_at: datetime) -> CostWindow:
        if end_at <= start_at:
            raise ValueError('end_at must be greater than start_at')
        return CostWindow(start_at=start_at, end_at=end_at)

    def summarize_overview(self, *, db: Session, window: CostWindow) -> dict[str, object]:
        # 目的：回傳公司層級的 token/cost 總覽。
        # 為什麼：管理者先看總盤，再下鑽群組/個人/代理者明細。
        turn_rows = self._load_turn_rows(db=db, window=window)
        company_bucket = self._empty_bucket()
        estimated_turns = 0
        for row in turn_rows:
            self._accumulate_bucket(company_bucket, row)
            if bool(row.get('estimated')):
                estimated_turns += 1

        return {
            'window': self._serialize_window(window),
            'company': {
                **company_bucket,
                'turns': len(turn_rows),
                'estimated_turns': int(estimated_turns),
            },
        }

    def list_breakdown(
        self,
        *,
        db: Session,
        window: CostWindow,
        dimension: str,
        limit: int,
        offset: int,
    ) -> dict[str, object]:
        # 目的：依指定維度輸出聚合明細（group/user/agent）。
        # 為什麼：四層治理要能從同一口徑切出不同責任歸屬視角。
        normalized_dimension = str(dimension or '').strip().lower()
        if normalized_dimension not in {'group', 'user', 'agent'}:
            raise ValueError('dimension must be one of group, user, agent')

        if self._can_use_postgres_sql_aggregation(db=db):
            return self._list_breakdown_via_sql(
                db=db,
                window=window,
                dimension=normalized_dimension,
                limit=limit,
                offset=offset,
            )

        turn_rows = self._load_turn_rows(db=db, window=window)
        if normalized_dimension == 'group':
            rows = self._group_breakdown_rows(db=db, turn_rows=turn_rows)
        elif normalized_dimension == 'user':
            rows = self._user_breakdown_rows(db=db, turn_rows=turn_rows)
        else:
            rows = self._agent_breakdown_rows(db=db, turn_rows=turn_rows)

        sorted_rows = sorted(
            rows,
            key=lambda row: (
                float(row.get('cost_usd') or 0.0),
                int(row.get('total_tokens') or 0),
                int(row.get('turns') or 0),
            ),
            reverse=True,
        )
        safe_offset = max(0, int(offset))
        safe_limit = max(1, int(limit))
        paged_rows = sorted_rows[safe_offset:safe_offset + safe_limit]
        return {
            'window': self._serialize_window(window),
            'dimension': normalized_dimension,
            'total': len(sorted_rows),
            'limit': safe_limit,
            'offset': safe_offset,
            'items': paged_rows,
        }

    def _list_breakdown_via_sql(
        self,
        *,
        db: Session,
        window: CostWindow,
        dimension: str,
        limit: int,
        offset: int,
    ) -> dict[str, object]:
        # 目的：使用 SQL 聚合與分頁直接取得明細。
        # 為什麼：避免 Python 端載入整月全量資料，降低高併發下記憶體與 CPU 壓力。
        safe_limit = max(1, int(limit))
        safe_offset = max(0, int(offset))
        total_tokens_expr = self._usage_total_tokens_expr()
        input_tokens_expr = self._usage_input_tokens_expr()
        output_tokens_expr = self._usage_output_tokens_expr()
        cost_expr = func.coalesce(LlmTurn.cost_usd, 0)
        estimated_turn_expr = self._usage_estimated_turn_expr()

        if dimension == 'user':
            grouped_query = (
                db.query(
                    Conversation.user_id.label('scope_id'),
                    User.username.label('scope_name'),
                    func.count(LlmTurn.id).label('turns'),
                    func.sum(input_tokens_expr).label('input_tokens'),
                    func.sum(output_tokens_expr).label('output_tokens'),
                    func.sum(total_tokens_expr).label('total_tokens'),
                    func.sum(cost_expr).label('cost_usd'),
                    func.sum(estimated_turn_expr).label('estimated_turns'),
                )
                .join(Conversation, Conversation.id == LlmTurn.conversation_id)
                .outerjoin(User, User.id == Conversation.user_id)
                .filter(
                    LlmTurn.created_at >= window.start_at,
                    LlmTurn.created_at < window.end_at,
                )
                .group_by(Conversation.user_id, User.username)
            )
            total_count = grouped_query.count()
            rows = (
                grouped_query
                .order_by(func.sum(cost_expr).desc(), func.sum(total_tokens_expr).desc(), func.count(LlmTurn.id).desc())
                .offset(safe_offset)
                .limit(safe_limit)
                .all()
            )
            items = [
                {
                    'user_id': str(row.scope_id) if row.scope_id else None,
                    'username': str(row.scope_name or '未知使用者'),
                    'turns': int(row.turns or 0),
                    'input_tokens': int(row.input_tokens or 0),
                    'output_tokens': int(row.output_tokens or 0),
                    'total_tokens': int(row.total_tokens or 0),
                    'cost_usd': round(float(row.cost_usd or 0), 6),
                    'estimated_turns': int(row.estimated_turns or 0),
                }
                for row in rows
            ]
            return {
                'window': self._serialize_window(window),
                'dimension': dimension,
                'total': int(total_count),
                'limit': safe_limit,
                'offset': safe_offset,
                'items': items,
            }

        if dimension == 'agent':
            grouped_query = (
                db.query(
                    LlmTurn.agent_id.label('scope_id'),
                    Agent.name.label('scope_name'),
                    func.count(LlmTurn.id).label('turns'),
                    func.sum(input_tokens_expr).label('input_tokens'),
                    func.sum(output_tokens_expr).label('output_tokens'),
                    func.sum(total_tokens_expr).label('total_tokens'),
                    func.sum(cost_expr).label('cost_usd'),
                    func.sum(estimated_turn_expr).label('estimated_turns'),
                )
                .outerjoin(Agent, Agent.id == LlmTurn.agent_id)
                .filter(
                    LlmTurn.created_at >= window.start_at,
                    LlmTurn.created_at < window.end_at,
                )
                .group_by(LlmTurn.agent_id, Agent.name)
            )
            total_count = grouped_query.count()
            rows = (
                grouped_query
                .order_by(func.sum(cost_expr).desc(), func.sum(total_tokens_expr).desc(), func.count(LlmTurn.id).desc())
                .offset(safe_offset)
                .limit(safe_limit)
                .all()
            )
            items = [
                {
                    'agent_id': str(row.scope_id),
                    'agent_name': str(row.scope_name or f'未知代理({str(row.scope_id)[:8]})'),
                    'turns': int(row.turns or 0),
                    'input_tokens': int(row.input_tokens or 0),
                    'output_tokens': int(row.output_tokens or 0),
                    'total_tokens': int(row.total_tokens or 0),
                    'cost_usd': round(float(row.cost_usd or 0), 6),
                    'estimated_turns': int(row.estimated_turns or 0),
                }
                for row in rows
            ]
            return {
                'window': self._serialize_window(window),
                'dimension': dimension,
                'total': int(total_count),
                'limit': safe_limit,
                'offset': safe_offset,
                'items': items,
            }

        grouped_query = (
            db.query(
                AccessGroup.id.label('scope_id'),
                AccessGroup.code.label('scope_code'),
                AccessGroup.name.label('scope_name'),
                func.count(LlmTurn.id).label('turns'),
                func.sum(input_tokens_expr).label('input_tokens'),
                func.sum(output_tokens_expr).label('output_tokens'),
                func.sum(total_tokens_expr).label('total_tokens'),
                func.sum(cost_expr).label('cost_usd'),
                func.sum(estimated_turn_expr).label('estimated_turns'),
            )
            .join(Conversation, Conversation.id == LlmTurn.conversation_id)
            .join(UserGroupBinding, UserGroupBinding.user_id == Conversation.user_id)
            .join(AccessGroup, AccessGroup.id == UserGroupBinding.group_id)
            .filter(
                LlmTurn.created_at >= window.start_at,
                LlmTurn.created_at < window.end_at,
                AccessGroup.enabled == True,  # noqa: E712
            )
            .group_by(AccessGroup.id, AccessGroup.code, AccessGroup.name)
        )
        total_count = grouped_query.count()
        rows = (
            grouped_query
            .order_by(func.sum(cost_expr).desc(), func.sum(total_tokens_expr).desc(), func.count(LlmTurn.id).desc())
            .offset(safe_offset)
            .limit(safe_limit)
            .all()
        )
        items = [
            {
                'group_id': str(row.scope_id),
                'group_code': str(row.scope_code or ''),
                'group_name': str(row.scope_name or ''),
                'turns': int(row.turns or 0),
                'input_tokens': int(row.input_tokens or 0),
                'output_tokens': int(row.output_tokens or 0),
                'total_tokens': int(row.total_tokens or 0),
                'cost_usd': round(float(row.cost_usd or 0), 6),
                'estimated_turns': int(row.estimated_turns or 0),
            }
            for row in rows
        ]
        return {
            'window': self._serialize_window(window),
            'dimension': dimension,
            'total': int(total_count),
            'limit': safe_limit,
            'offset': safe_offset,
            'items': items,
        }

    def get_user_monthly_usage(
        self,
        *,
        db: Session,
        user_id: str,
        month_start: datetime,
        month_end: datetime,
    ) -> dict[str, object]:
        # 目的：提供登入使用者個人月用量查詢。
        # 為什麼：個人可見性是成本自主管理與預算治理的第一層防線。
        parsed_user_id = self._parse_uuid_or_none(user_id)
        if parsed_user_id is None:
            raise ValueError('invalid user_id')
        window = self.build_window(start_at=month_start, end_at=month_end)
        turn_rows = self._load_turn_rows(db=db, window=window)
        bucket = self._empty_bucket()
        estimated_turns = 0
        for row in turn_rows:
            row_user_id = self._parse_uuid_or_none(str(row.get('user_id') or ''))
            if row_user_id != parsed_user_id:
                continue
            self._accumulate_bucket(bucket, row)
            if bool(row.get('estimated')):
                estimated_turns += 1

        user_row = db.query(User).filter(User.id == parsed_user_id).first()
        return {
            'window': self._serialize_window(window),
            'user': {
                'user_id': str(parsed_user_id),
                'username': str(getattr(user_row, 'username', '') or ''),
            },
            'usage': {
                **bucket,
                'estimated_turns': int(estimated_turns),
            },
        }

    def list_policies(self, *, db: Session) -> list[dict[str, object]]:
        # 目的：列出所有成本政策設定。
        # 為什麼：前端與管理流程需要固定來源讀取四層預算與模式。
        policy_rows = db.query(LlmCostPolicy).order_by(LlmCostPolicy.scope_type.asc(), LlmCostPolicy.created_at.asc()).all()
        return [self._serialize_policy(policy_row) for policy_row in policy_rows]

    def upsert_policies(self, *, db: Session, policies: list[dict[str, object]]) -> list[dict[str, object]]:
        # 目的：批次建立或更新四層成本政策。
        # 為什麼：治理政策調整多半同時跨多層，需支援單次提交一致寫入。
        output_rows: list[LlmCostPolicy] = []
        for raw_policy in policies:
            normalized_policy = self._normalize_policy_input(raw_policy)
            scope_type = str(normalized_policy['scope_type'])
            scope_id = normalized_policy['scope_id']
            row = (
                db.query(LlmCostPolicy)
                .filter(
                    LlmCostPolicy.scope_type == scope_type,
                    LlmCostPolicy.scope_id == scope_id,
                )
                .first()
            )
            if row is None:
                row = LlmCostPolicy(scope_type=scope_type, scope_id=scope_id)
                db.add(row)

            row.enabled = bool(normalized_policy['enabled'])
            row.enforcement_mode = str(normalized_policy['enforcement_mode'])
            row.monthly_input_tokens_limit = normalized_policy['monthly_input_tokens_limit']
            row.monthly_total_tokens_limit = normalized_policy['monthly_total_tokens_limit']
            row.monthly_cost_usd_limit = normalized_policy['monthly_cost_usd_limit']
            row.warn_thresholds = list(normalized_policy['warn_thresholds'])
            output_rows.append(row)

        db.commit()
        for output_row in output_rows:
            db.refresh(output_row)
        return [self._serialize_policy(row) for row in output_rows]

    def evaluate_monthly_alerts(self, *, db: Session, month_start: datetime, month_end: datetime) -> dict[str, object]:
        # 目的：依目前政策計算月份告警並去重寫入事件。
        # 為什麼：warn-only 階段需先建立可觀測告警，避免未成熟即硬阻擋。
        window = self.build_window(start_at=month_start, end_at=month_end)
        turn_rows = self._load_turn_rows(db=db, window=window)
        user_rows = self._user_breakdown_rows(db=db, turn_rows=turn_rows)
        group_rows = self._group_breakdown_rows(db=db, turn_rows=turn_rows)
        agent_rows = self._agent_breakdown_rows(db=db, turn_rows=turn_rows)
        company_bucket = self._empty_bucket()
        for turn_row in turn_rows:
            self._accumulate_bucket(company_bucket, turn_row)

        policy_rows = db.query(LlmCostPolicy).filter(LlmCostPolicy.enabled == True).all()  # noqa: E712
        user_map = {str(row.get('user_id') or ''): row for row in user_rows}
        group_map = {str(row.get('group_id') or ''): row for row in group_rows}
        agent_map = {str(row.get('agent_id') or ''): row for row in agent_rows}

        created_alerts: list[dict[str, object]] = []
        for policy_row in policy_rows:
            usage_bucket = self._resolve_policy_usage_bucket(
                policy_row=policy_row,
                company_bucket=company_bucket,
                user_map=user_map,
                group_map=group_map,
                agent_map=agent_map,
            )
            created_alerts.extend(
                self._create_policy_threshold_alerts(
                    db=db,
                    policy_row=policy_row,
                    usage_bucket=usage_bucket,
                    window=window,
                )
            )

        db.commit()
        return {
            'window': self._serialize_window(window),
            'created_count': len(created_alerts),
            'created_items': created_alerts,
        }

    def list_alert_events(
        self,
        *,
        db: Session,
        month_start: datetime,
        month_end: datetime,
        limit: int,
        offset: int,
    ) -> dict[str, object]:
        # 目的：查詢指定月份告警事件。
        # 為什麼：管理者需要查核門檻觸發時間與責任層級，支持追蹤與排障。
        safe_limit = max(1, int(limit))
        safe_offset = max(0, int(offset))
        window = self.build_window(start_at=month_start, end_at=month_end)
        query = db.query(LlmCostAlertEvent).filter(
            and_(
                LlmCostAlertEvent.window_start == window.start_at,
                LlmCostAlertEvent.window_end == window.end_at,
            )
        )
        total_count = query.count()
        rows = (
            query.order_by(LlmCostAlertEvent.created_at.desc())
            .offset(safe_offset)
            .limit(safe_limit)
            .all()
        )
        return {
            'window': self._serialize_window(window),
            'total': int(total_count),
            'limit': safe_limit,
            'offset': safe_offset,
            'items': [self._serialize_alert(row) for row in rows],
        }

    def evaluate_monthly_guard(
        self,
        *,
        db: Session,
        user_id: str,
        agent_id: str,
        now: datetime | None = None,
    ) -> CostGuardDecision:
        # 目的：在 LLM 呼叫前判斷四層 hard-limit 是否已超限。
        # 為什麼：成本治理最終防線需在執行前攔截，避免超支後才補救。
        reference_time = now or datetime.now()
        month_start = datetime(reference_time.year, reference_time.month, 1)
        month_end = (
            datetime(reference_time.year + 1, 1, 1)
            if reference_time.month == 12
            else datetime(reference_time.year, reference_time.month + 1, 1)
        )
        window = self.build_window(start_at=month_start, end_at=month_end)

        turn_rows = self._load_turn_rows(db=db, window=window)
        company_bucket = self._empty_bucket()
        for turn_row in turn_rows:
            self._accumulate_bucket(company_bucket, turn_row)
        user_rows = self._user_breakdown_rows(db=db, turn_rows=turn_rows)
        group_rows = self._group_breakdown_rows(db=db, turn_rows=turn_rows)
        agent_rows = self._agent_breakdown_rows(db=db, turn_rows=turn_rows)

        user_map = {str(row.get('user_id') or ''): row for row in user_rows}
        group_map = {str(row.get('group_id') or ''): row for row in group_rows}
        agent_map = {str(row.get('agent_id') or ''): row for row in agent_rows}
        target_user_id = str(self._parse_uuid_or_none(user_id) or '')
        target_agent_id = str(self._parse_uuid_or_none(agent_id) or '')
        target_group_ids = self._load_target_group_ids(db=db, user_id=target_user_id)

        hard_limit_rows = (
            db.query(LlmCostPolicy)
            .filter(
                LlmCostPolicy.enabled == True,  # noqa: E712
                LlmCostPolicy.enforcement_mode == 'hard_limit',
            )
            .order_by(LlmCostPolicy.scope_type.asc(), LlmCostPolicy.created_at.asc())
            .all()
        )

        for policy_row in hard_limit_rows:
            if not self._policy_applies_to_target(
                policy_row=policy_row,
                user_id=target_user_id,
                agent_id=target_agent_id,
                group_ids=target_group_ids,
            ):
                continue

            usage_bucket = self._resolve_policy_usage_bucket(
                policy_row=policy_row,
                company_bucket=company_bucket,
                user_map=user_map,
                group_map=group_map,
                agent_map=agent_map,
            )
            hit_result = self._first_limit_hit(policy_row=policy_row, usage_bucket=usage_bucket)
            if hit_result is None:
                continue

            metric_key, current_value, limit_value, usage_percent = hit_result
            self._create_or_update_block_alert(
                db=db,
                policy_row=policy_row,
                metric_key=metric_key,
                current_value=current_value,
                limit_value=limit_value,
                usage_percent=usage_percent,
                window=window,
            )
            db.commit()
            return CostGuardDecision(
                allowed=False,
                reason=self._build_guard_reason(scope_type=str(policy_row.scope_type or ''), metric_key=metric_key),
                scope_type=str(policy_row.scope_type or ''),
                scope_id=str(policy_row.scope_id) if policy_row.scope_id else None,
                metric_key=metric_key,
                current_value=float(current_value),
                limit_value=float(limit_value),
                usage_percent=float(self._round_decimal(usage_percent, '0.01')),
            )

        return CostGuardDecision(
            allowed=True,
            reason='pass',
            scope_type=None,
            scope_id=None,
            metric_key=None,
            current_value=0.0,
            limit_value=0.0,
            usage_percent=0.0,
        )

    def _normalize_policy_input(self, raw_policy: dict[str, object]) -> dict[str, object]:
        # 目的：驗證並標準化政策輸入格式。
        # 為什麼：避免無效層級或負值門檻寫入資料庫，造成治理判斷失真。
        scope_type = str((raw_policy or {}).get('scope_type') or '').strip().lower()
        if scope_type not in {'company', 'group', 'user', 'agent'}:
            raise ValueError('scope_type must be one of company, group, user, agent')

        scope_id = self._normalize_scope_id(scope_type=scope_type, raw_scope_id=(raw_policy or {}).get('scope_id'))
        enabled = bool((raw_policy or {}).get('enabled', True))

        enforcement_mode = str((raw_policy or {}).get('enforcement_mode') or 'warn_only').strip().lower()
        if enforcement_mode not in {'warn_only', 'hard_limit'}:
            raise ValueError('enforcement_mode must be warn_only or hard_limit')

        monthly_input_tokens_limit = self._parse_positive_int_or_none((raw_policy or {}).get('monthly_input_tokens_limit'))
        monthly_total_tokens_limit = self._parse_positive_int_or_none((raw_policy or {}).get('monthly_total_tokens_limit'))
        monthly_cost_usd_limit = self._parse_positive_decimal_or_none((raw_policy or {}).get('monthly_cost_usd_limit'))

        warn_thresholds = self._normalize_warn_thresholds((raw_policy or {}).get('warn_thresholds'))
        if (
            monthly_input_tokens_limit is None
            and monthly_total_tokens_limit is None
            and monthly_cost_usd_limit is None
        ):
            raise ValueError('at least one monthly limit is required')

        return {
            'scope_type': scope_type,
            'scope_id': scope_id,
            'enabled': enabled,
            'enforcement_mode': enforcement_mode,
            'monthly_input_tokens_limit': monthly_input_tokens_limit,
            'monthly_total_tokens_limit': monthly_total_tokens_limit,
            'monthly_cost_usd_limit': monthly_cost_usd_limit,
            'warn_thresholds': warn_thresholds,
        }

    def _normalize_scope_id(self, *, scope_type: str, raw_scope_id: object) -> uuid.UUID | None:
        # 目的：依層級規則驗證 scope_id。
        # 為什麼：company 層不得帶 scope_id，其餘層必須是合法 UUID。
        if scope_type == 'company':
            return None
        parsed_scope_id = self._parse_uuid_or_none(str(raw_scope_id or ''))
        if parsed_scope_id is None:
            raise ValueError('scope_id must be valid UUID for group/user/agent policy')
        return parsed_scope_id

    def _normalize_warn_thresholds(self, raw_thresholds: object) -> list[int]:
        # 目的：標準化告警門檻百分比列表。
        # 為什麼：前端可能傳入字串或重複值，需在後端統一去重排序。
        thresholds_raw = raw_thresholds if isinstance(raw_thresholds, list) else [50, 80, 100]
        normalized: list[int] = []
        for threshold_item in thresholds_raw:
            try:
                parsed = int(threshold_item)
            except Exception:
                continue
            if parsed <= 0 or parsed > 100:
                continue
            normalized.append(parsed)
        unique_sorted = sorted(set(normalized))
        if not unique_sorted:
            return [50, 80, 100]
        return unique_sorted

    def _resolve_policy_usage_bucket(
        self,
        *,
        policy_row: LlmCostPolicy,
        company_bucket: dict[str, object],
        user_map: dict[str, dict[str, object]],
        group_map: dict[str, dict[str, object]],
        agent_map: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        # 目的：依政策層級取得對應用量資料。
        # 為什麼：告警判斷需先映射到單一責任主體，才能計算使用率。
        scope_type = str(policy_row.scope_type or '').strip().lower()
        scope_id_text = str(policy_row.scope_id) if policy_row.scope_id else ''
        if scope_type == 'company':
            return company_bucket
        if scope_type == 'user':
            return user_map.get(scope_id_text, self._empty_bucket())
        if scope_type == 'group':
            return group_map.get(scope_id_text, self._empty_bucket())
        if scope_type == 'agent':
            return agent_map.get(scope_id_text, self._empty_bucket())
        return self._empty_bucket()

    def _create_policy_threshold_alerts(
        self,
        *,
        db: Session,
        policy_row: LlmCostPolicy,
        usage_bucket: dict[str, object],
        window: CostWindow,
    ) -> list[dict[str, object]]:
        # 目的：為單一政策產生門檻告警並去重。
        # 為什麼：同一月份門檻只需告警一次，避免重複噪音淹沒真實風險。
        metric_definitions = [
            ('monthly_input_tokens', 'input_tokens', self._parse_positive_decimal_or_none(policy_row.monthly_input_tokens_limit)),
            ('monthly_total_tokens', 'total_tokens', self._parse_positive_decimal_or_none(policy_row.monthly_total_tokens_limit)),
            ('monthly_cost_usd', 'cost_usd', self._parse_positive_decimal_or_none(policy_row.monthly_cost_usd_limit)),
        ]
        policy_thresholds = self._normalize_warn_thresholds(policy_row.warn_thresholds)
        created_alerts: list[dict[str, object]] = []
        for metric_key, usage_key, limit_value in metric_definitions:
            if limit_value is None or limit_value <= 0:
                continue
            current_value = self._parse_positive_decimal_or_none(usage_bucket.get(usage_key), allow_zero=True)
            if current_value is None:
                current_value = Decimal('0')
            usage_percent = (current_value / limit_value) * Decimal('100')
            for threshold_percent in policy_thresholds:
                if usage_percent < Decimal(threshold_percent):
                    continue
                existed = (
                    db.query(LlmCostAlertEvent)
                    .filter(
                        LlmCostAlertEvent.policy_id == policy_row.id,
                        LlmCostAlertEvent.window_start == window.start_at,
                        LlmCostAlertEvent.metric_key == metric_key,
                        LlmCostAlertEvent.threshold_percent == int(threshold_percent),
                    )
                    .first()
                )
                if existed is not None:
                    continue

                status = 'blocked' if str(policy_row.enforcement_mode or 'warn_only') == 'hard_limit' and threshold_percent >= 100 else 'warned'
                alert_row = LlmCostAlertEvent(
                    policy_id=policy_row.id,
                    scope_type=str(policy_row.scope_type or ''),
                    scope_id=policy_row.scope_id,
                    metric_key=metric_key,
                    threshold_percent=int(threshold_percent),
                    current_value=current_value,
                    limit_value=limit_value,
                    usage_percent=self._round_decimal(usage_percent, '0.01'),
                    enforcement_mode=str(policy_row.enforcement_mode or 'warn_only'),
                    status=status,
                    window_start=window.start_at,
                    window_end=window.end_at,
                    details={'usage_key': usage_key},
                )
                db.add(alert_row)
                db.flush()
                created_alerts.append(self._serialize_alert(alert_row))
        return created_alerts

    def _load_target_group_ids(self, *, db: Session, user_id: str) -> set[str]:
        # 目的：載入指定使用者的群組 ID 集合。
        # 為什麼：判斷群組層政策是否適用時，需快速比對使用者歸屬。
        parsed_user_id = self._parse_uuid_or_none(user_id)
        if parsed_user_id is None:
            return set()
        rows = (
            db.query(UserGroupBinding.group_id)
            .join(AccessGroup, AccessGroup.id == UserGroupBinding.group_id)
            .filter(
                UserGroupBinding.user_id == parsed_user_id,
                AccessGroup.enabled == True,  # noqa: E712
            )
            .all()
        )
        return {str(row.group_id) for row in rows if getattr(row, 'group_id', None) is not None}

    def _policy_applies_to_target(
        self,
        *,
        policy_row: LlmCostPolicy,
        user_id: str,
        agent_id: str,
        group_ids: set[str],
    ) -> bool:
        # 目的：判斷政策是否適用於當前請求目標。
        # 為什麼：四層政策同時存在時，僅應套用與目前 user/agent 關聯的條目。
        scope_type = str(policy_row.scope_type or '').strip().lower()
        scope_id_text = str(policy_row.scope_id) if policy_row.scope_id else ''
        if scope_type == 'company':
            return True
        if scope_type == 'user':
            return bool(scope_id_text) and scope_id_text == str(user_id or '')
        if scope_type == 'agent':
            return bool(scope_id_text) and scope_id_text == str(agent_id or '')
        if scope_type == 'group':
            return bool(scope_id_text) and scope_id_text in group_ids
        return False

    def _first_limit_hit(
        self,
        *,
        policy_row: LlmCostPolicy,
        usage_bucket: dict[str, object],
    ) -> tuple[str, Decimal, Decimal, Decimal] | None:
        # 目的：找出政策第一個命中的超限指標。
        # 為什麼：拒絕回應需要單一主要原因碼，避免多重原因造成前端判讀複雜。
        metric_checks = [
            ('monthly_input_tokens', 'input_tokens', self._parse_positive_decimal_or_none(policy_row.monthly_input_tokens_limit)),
            ('monthly_total_tokens', 'total_tokens', self._parse_positive_decimal_or_none(policy_row.monthly_total_tokens_limit)),
            ('monthly_cost_usd', 'cost_usd', self._parse_positive_decimal_or_none(policy_row.monthly_cost_usd_limit)),
        ]
        for metric_key, usage_key, limit_value in metric_checks:
            if limit_value is None or limit_value <= 0:
                continue
            current_value = self._parse_positive_decimal_or_none(usage_bucket.get(usage_key), allow_zero=True)
            if current_value is None:
                current_value = Decimal('0')
            if current_value < limit_value:
                continue
            usage_percent = (current_value / limit_value) * Decimal('100')
            return metric_key, current_value, limit_value, usage_percent
        return None

    def _create_or_update_block_alert(
        self,
        *,
        db: Session,
        policy_row: LlmCostPolicy,
        metric_key: str,
        current_value: Decimal,
        limit_value: Decimal,
        usage_percent: Decimal,
        window: CostWindow,
    ) -> None:
        # 目的：在 hard-limit 命中時建立或更新 blocked 告警事件。
        # 為什麼：即時拒絕需保留事件證據，支援後續稽核與營運排障。
        threshold_percent = 100
        existed = (
            db.query(LlmCostAlertEvent)
            .filter(
                LlmCostAlertEvent.policy_id == policy_row.id,
                LlmCostAlertEvent.window_start == window.start_at,
                LlmCostAlertEvent.metric_key == metric_key,
                LlmCostAlertEvent.threshold_percent == threshold_percent,
            )
            .first()
        )
        if existed is None:
            db.add(
                LlmCostAlertEvent(
                    policy_id=policy_row.id,
                    scope_type=str(policy_row.scope_type or ''),
                    scope_id=policy_row.scope_id,
                    metric_key=metric_key,
                    threshold_percent=threshold_percent,
                    current_value=current_value,
                    limit_value=limit_value,
                    usage_percent=self._round_decimal(usage_percent, '0.01'),
                    enforcement_mode='hard_limit',
                    status='blocked',
                    window_start=window.start_at,
                    window_end=window.end_at,
                    details={'source': 'preflight_guard'},
                )
            )
            return

        existed.current_value = current_value
        existed.limit_value = limit_value
        existed.usage_percent = self._round_decimal(usage_percent, '0.01')
        existed.enforcement_mode = 'hard_limit'
        existed.status = 'blocked'
        existed.details = {'source': 'preflight_guard'}

    def _build_guard_reason(self, *, scope_type: str, metric_key: str) -> str:
        # 目的：產生一致的拒絕原因碼。
        # 為什麼：前端與稽核需要穩定鍵值做告警分類與訊息對照。
        normalized_scope_type = str(scope_type or '').strip().lower()
        normalized_metric_key = str(metric_key or '').strip().lower()
        if normalized_metric_key == 'monthly_input_tokens':
            return f'quota_{normalized_scope_type}_monthly_input_tokens_exceeded'
        if normalized_metric_key == 'monthly_total_tokens':
            return f'quota_{normalized_scope_type}_monthly_total_tokens_exceeded'
        if normalized_metric_key == 'monthly_cost_usd':
            return f'quota_{normalized_scope_type}_monthly_cost_usd_exceeded'
        return f'quota_{normalized_scope_type}_monthly_limit_exceeded'

    def _serialize_policy(self, row: LlmCostPolicy) -> dict[str, object]:
        return {
            'id': str(row.id),
            'scope_type': str(row.scope_type or ''),
            'scope_id': str(row.scope_id) if row.scope_id else None,
            'enabled': bool(row.enabled),
            'enforcement_mode': str(row.enforcement_mode or 'warn_only'),
            'monthly_input_tokens_limit': int(row.monthly_input_tokens_limit) if row.monthly_input_tokens_limit else None,
            'monthly_total_tokens_limit': int(row.monthly_total_tokens_limit) if row.monthly_total_tokens_limit else None,
            'monthly_cost_usd_limit': float(row.monthly_cost_usd_limit) if row.monthly_cost_usd_limit is not None else None,
            'warn_thresholds': self._normalize_warn_thresholds(row.warn_thresholds),
            'updated_at': row.updated_at.isoformat() if row.updated_at else None,
        }

    def _serialize_alert(self, row: LlmCostAlertEvent) -> dict[str, object]:
        return {
            'id': str(row.id),
            'policy_id': str(row.policy_id),
            'scope_type': str(row.scope_type or ''),
            'scope_id': str(row.scope_id) if row.scope_id else None,
            'metric_key': str(row.metric_key or ''),
            'threshold_percent': int(row.threshold_percent or 0),
            'current_value': float(row.current_value),
            'limit_value': float(row.limit_value),
            'usage_percent': float(row.usage_percent),
            'enforcement_mode': str(row.enforcement_mode or 'warn_only'),
            'status': str(row.status or 'warned'),
            'window_start': row.window_start.isoformat() if row.window_start else None,
            'window_end': row.window_end.isoformat() if row.window_end else None,
            'details': row.details if isinstance(row.details, dict) else {},
            'created_at': row.created_at.isoformat() if row.created_at else None,
        }

    def _load_turn_rows(self, *, db: Session, window: CostWindow) -> list[dict[str, object]]:
        # 目的：讀取指定區間內 LLM 呼叫資料並標準化欄位。
        # 為什麼：不同供應商 usage 格式不一，聚合前需先統一資料結構。
        raw_rows = (
            db.query(
                LlmTurn.id,
                LlmTurn.agent_id,
                LlmTurn.conversation_id,
                LlmTurn.usage,
                LlmTurn.cost_usd,
                LlmTurn.created_at,
                Conversation.user_id,
            )
            .join(Conversation, Conversation.id == LlmTurn.conversation_id)
            .filter(
                LlmTurn.created_at >= window.start_at,
                LlmTurn.created_at < window.end_at,
            )
            .all()
        )
        rows: list[dict[str, object]] = []
        for raw_row in raw_rows:
            usage_dict = raw_row.usage if isinstance(raw_row.usage, dict) else {}
            input_tokens = self._to_int(usage_dict.get('input_tokens'))
            output_tokens = self._to_int(usage_dict.get('output_tokens'))
            total_tokens = self._to_int(usage_dict.get('total_tokens'))
            if total_tokens <= 0:
                total_tokens = input_tokens + output_tokens
            if input_tokens <= 0 and total_tokens > 0 and output_tokens > 0:
                input_tokens = max(0, total_tokens - output_tokens)
            if output_tokens <= 0 and total_tokens > 0 and input_tokens > 0:
                output_tokens = max(0, total_tokens - input_tokens)

            cost_decimal = self._to_decimal_or_zero(raw_row.cost_usd)
            estimated = self._is_estimated_usage(usage_dict=usage_dict)
            rows.append(
                {
                    'turn_id': str(raw_row.id),
                    'agent_id': str(raw_row.agent_id),
                    'conversation_id': str(raw_row.conversation_id),
                    'user_id': str(raw_row.user_id) if raw_row.user_id else None,
                    'input_tokens': int(input_tokens),
                    'output_tokens': int(output_tokens),
                    'total_tokens': int(total_tokens),
                    'cost_usd': float(cost_decimal),
                    'estimated': bool(estimated),
                    'created_at': raw_row.created_at.isoformat() if raw_row.created_at else None,
                }
            )
        return rows

    def _user_breakdown_rows(self, *, db: Session, turn_rows: list[dict[str, object]]) -> list[dict[str, object]]:
        # 目的：計算個人層級聚合並回填使用者名稱。
        # 為什麼：儀表板主要由人閱讀，僅顯示 UUID 不利快速定位責任人。
        user_name_map = {
            str(user_row.id): str(user_row.username or '')
            for user_row in db.query(User.id, User.username).all()
        }
        user_buckets: dict[str, dict[str, object]] = {}
        for row in turn_rows:
            user_id = str(row.get('user_id') or '').strip()
            if not user_id:
                user_id = '__unknown_user__'
            if user_id not in user_buckets:
                user_buckets[user_id] = {
                    'user_id': None if user_id == '__unknown_user__' else user_id,
                    'username': '未知使用者' if user_id == '__unknown_user__' else str(user_name_map.get(user_id) or user_id[:8]),
                    **self._empty_bucket(),
                    'turns': 0,
                    'estimated_turns': 0,
                }
            self._accumulate_bucket(user_buckets[user_id], row)
            user_buckets[user_id]['turns'] = int(user_buckets[user_id]['turns']) + 1
            if bool(row.get('estimated')):
                user_buckets[user_id]['estimated_turns'] = int(user_buckets[user_id]['estimated_turns']) + 1
        return list(user_buckets.values())

    def _agent_breakdown_rows(self, *, db: Session, turn_rows: list[dict[str, object]]) -> list[dict[str, object]]:
        agent_map = {
            str(agent_row.id): str(agent_row.name or '')
            for agent_row in db.query(Agent.id, Agent.name).all()
        }
        agent_buckets: dict[str, dict[str, object]] = {}
        for row in turn_rows:
            agent_id = str(row.get('agent_id') or '').strip()
            if not agent_id:
                continue
            if agent_id not in agent_buckets:
                agent_buckets[agent_id] = {
                    'agent_id': agent_id,
                    'agent_name': agent_map.get(agent_id) or f'未知代理({agent_id[:8]})',
                    **self._empty_bucket(),
                    'turns': 0,
                    'estimated_turns': 0,
                }
            self._accumulate_bucket(agent_buckets[agent_id], row)
            agent_buckets[agent_id]['turns'] = int(agent_buckets[agent_id]['turns']) + 1
            if bool(row.get('estimated')):
                agent_buckets[agent_id]['estimated_turns'] = int(agent_buckets[agent_id]['estimated_turns']) + 1
        return list(agent_buckets.values())

    def _group_breakdown_rows(self, *, db: Session, turn_rows: list[dict[str, object]]) -> list[dict[str, object]]:
        # 目的：計算群組層級聚合，採多群組全額計入策略。
        # 為什麼：治理上需保守反映風險，讓每個相關群組都能看到完整使用量。
        user_group_map = self._load_user_group_map(db=db)
        group_buckets: dict[str, dict[str, object]] = {}
        for row in turn_rows:
            user_id = str(row.get('user_id') or '').strip()
            if not user_id:
                continue
            user_groups = user_group_map.get(user_id, [])
            for group_item in user_groups:
                group_id = str(group_item.get('group_id') or '').strip()
                if not group_id:
                    continue
                if group_id not in group_buckets:
                    group_buckets[group_id] = {
                        'group_id': group_id,
                        'group_code': str(group_item.get('group_code') or ''),
                        'group_name': str(group_item.get('group_name') or ''),
                        **self._empty_bucket(),
                        'turns': 0,
                        'estimated_turns': 0,
                    }
                self._accumulate_bucket(group_buckets[group_id], row)
                group_buckets[group_id]['turns'] = int(group_buckets[group_id]['turns']) + 1
                if bool(row.get('estimated')):
                    group_buckets[group_id]['estimated_turns'] = int(group_buckets[group_id]['estimated_turns']) + 1
        return list(group_buckets.values())

    def _load_user_group_map(self, *, db: Session) -> dict[str, list[dict[str, str]]]:
        # 目的：載入使用者所屬群組對照。
        # 為什麼：群組層統計需將每筆使用量映射到使用者所屬群組。
        rows = (
            db.query(UserGroupBinding.user_id, AccessGroup.id, AccessGroup.code, AccessGroup.name)
            .join(AccessGroup, AccessGroup.id == UserGroupBinding.group_id)
            .filter(AccessGroup.enabled == True)  # noqa: E712
            .all()
        )
        output: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            user_id = str(row.user_id)
            if user_id not in output:
                output[user_id] = []
            output[user_id].append(
                {
                    'group_id': str(row.id),
                    'group_code': str(row.code or ''),
                    'group_name': str(row.name or ''),
                }
            )
        return output

    def _is_estimated_usage(self, *, usage_dict: dict[str, object]) -> bool:
        raw = usage_dict.get('raw') if isinstance(usage_dict, dict) else None
        if not isinstance(raw, dict):
            return True
        for key in ('cost_usd', 'cost'):
            if raw.get(key) is not None:
                return False
        return True

    def _empty_bucket(self) -> dict[str, object]:
        return {
            'input_tokens': 0,
            'output_tokens': 0,
            'total_tokens': 0,
            'cost_usd': 0.0,
        }

    def _accumulate_bucket(self, bucket: dict[str, object], row: dict[str, object]) -> None:
        bucket['input_tokens'] = int(bucket.get('input_tokens') or 0) + int(row.get('input_tokens') or 0)
        bucket['output_tokens'] = int(bucket.get('output_tokens') or 0) + int(row.get('output_tokens') or 0)
        bucket['total_tokens'] = int(bucket.get('total_tokens') or 0) + int(row.get('total_tokens') or 0)
        bucket['cost_usd'] = round(float(bucket.get('cost_usd') or 0.0) + float(row.get('cost_usd') or 0.0), 6)

    def _serialize_window(self, window: CostWindow) -> dict[str, str]:
        return {
            'start_at': window.start_at.isoformat(),
            'end_at': window.end_at.isoformat(),
        }

    def _to_int(self, raw_value: object) -> int:
        try:
            return max(0, int(raw_value))
        except Exception:
            return 0

    def _to_decimal_or_zero(self, raw_value: object) -> Decimal:
        try:
            if raw_value is None:
                return Decimal('0')
            return Decimal(str(raw_value))
        except Exception:
            return Decimal('0')

    def _can_use_postgres_sql_aggregation(self, *, db: Session) -> bool:
        try:
            bind = db.get_bind()
            if bind is None:
                return False
            return str(bind.dialect.name or '').strip().lower() == 'postgresql'
        except Exception:
            return False

    def _usage_input_tokens_expr(self):
        usage_input_text = LlmTurn.usage.op('->>')('input_tokens')
        return func.coalesce(cast(usage_input_text, Integer), 0)

    def _usage_output_tokens_expr(self):
        usage_output_text = LlmTurn.usage.op('->>')('output_tokens')
        return func.coalesce(cast(usage_output_text, Integer), 0)

    def _usage_total_tokens_expr(self):
        usage_total_text = LlmTurn.usage.op('->>')('total_tokens')
        input_expr = self._usage_input_tokens_expr()
        output_expr = self._usage_output_tokens_expr()
        return func.coalesce(cast(usage_total_text, Integer), input_expr + output_expr)

    def _usage_estimated_turn_expr(self):
        raw_json = LlmTurn.usage.op('->')('raw')
        raw_cost_usd_text = raw_json.op('->>')('cost_usd')
        raw_cost_text = raw_json.op('->>')('cost')
        return cast(case((and_(raw_cost_usd_text.is_(None), raw_cost_text.is_(None)), 1), else_=0), Integer)

    def _parse_positive_decimal_or_none(self, raw_value: object, *, allow_zero: bool = False) -> Decimal | None:
        try:
            if raw_value is None:
                return None
            parsed = Decimal(str(raw_value))
        except Exception:
            return None
        if allow_zero:
            if parsed < 0:
                return None
            return parsed
        if parsed <= 0:
            return None
        return parsed

    def _parse_positive_int_or_none(self, raw_value: object) -> int | None:
        try:
            if raw_value is None:
                return None
            parsed = int(raw_value)
        except Exception:
            return None
        if parsed <= 0:
            return None
        return parsed

    def _round_decimal(self, value: Decimal, precision: str) -> Decimal:
        try:
            return value.quantize(Decimal(precision))
        except Exception:
            return value

    def _parse_uuid_or_none(self, raw_value: str) -> uuid.UUID | None:
        text = str(raw_value or '').strip()
        if not text:
            return None
        try:
            return uuid.UUID(text)
        except Exception:
            return None


cost_usage_service = CostUsageService()
