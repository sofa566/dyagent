from __future__ import annotations

from datetime import datetime, timedelta

from src.models import (
    AccessGroup,
    SkillEntry,
    ToolExecutionAudit,
    ToolExecutionConfirmation,
    UserGroupBinding,
)
from src.services.tool_policy_service import ToolPolicyService


class TestToolPolicyService:
    def test_dangerous_tool_requires_confirmation_token_and_single_use(self, db, regular_user):
        service = ToolPolicyService()
        skill = SkillEntry(
            name='dangerous-skill',
            description='dangerous',
            enabled=True,
            type='python',
            execution_policy={
                'risk_level': 'dangerous',
                'requires_confirmation': True,
                'cost_class': 'billable',
            },
        )
        db.add(skill)
        db.commit()

        denied_decision = service.evaluate_execution(
            db=db,
            tool_name='dangerous-skill',
            payload={},
            agent_class='tasked',
            user_id=str(regular_user.id),
        )
        assert denied_decision.passed is False
        assert denied_decision.reason == 'confirmation_required'
        assert isinstance(denied_decision.confirmation_token, str)
        assert denied_decision.confirmation_token

        passed_decision = service.evaluate_execution(
            db=db,
            tool_name='dangerous-skill',
            payload={'_policy_confirm_token': denied_decision.confirmation_token},
            agent_class='tasked',
            user_id=str(regular_user.id),
        )
        assert passed_decision.passed is True
        assert passed_decision.reason == 'pass'

        replay_decision = service.evaluate_execution(
            db=db,
            tool_name='dangerous-skill',
            payload={'_policy_confirm_token': denied_decision.confirmation_token},
            agent_class='tasked',
            user_id=str(regular_user.id),
        )
        assert replay_decision.passed is False
        assert replay_decision.reason == 'confirmation_required'

    def test_confirmation_token_expired_requires_new_confirmation(self, db, regular_user):
        service = ToolPolicyService()
        skill = SkillEntry(
            name='dangerous-skill-expired',
            description='dangerous',
            enabled=True,
            type='python',
            execution_policy={
                'risk_level': 'dangerous',
                'requires_confirmation': True,
            },
        )
        db.add(skill)
        db.commit()

        first_denied = service.evaluate_execution(
            db=db,
            tool_name='dangerous-skill-expired',
            payload={},
            agent_class='tasked',
            user_id=str(regular_user.id),
        )
        first_token = str(first_denied.confirmation_token or '')
        assert first_denied.passed is False
        assert first_denied.reason == 'confirmation_required'
        assert first_token

        token_hash = service._hash_token(first_token)
        row = db.query(ToolExecutionConfirmation).filter(ToolExecutionConfirmation.token_hash == token_hash).first()
        assert row is not None
        row.expires_at = datetime.now() - timedelta(seconds=1)
        db.add(row)
        db.commit()

        expired_decision = service.evaluate_execution(
            db=db,
            tool_name='dangerous-skill-expired',
            payload={'_policy_confirm_token': first_token},
            agent_class='tasked',
            user_id=str(regular_user.id),
        )
        assert expired_decision.passed is False
        assert expired_decision.reason == 'confirmation_required'
        assert str(expired_decision.confirmation_token or '') != first_token

    def test_daily_quota_blocks_after_limit(self, db, regular_user):
        service = ToolPolicyService()
        skill = SkillEntry(
            name='quota-skill',
            description='quota',
            enabled=True,
            type='python',
            execution_policy={
                'risk_level': 'restricted',
                'rate_limit_profile': {
                    'per_user_daily_calls': 1,
                },
            },
        )
        db.add(skill)
        db.flush()

        db.add(ToolExecutionAudit(
            user_id=regular_user.id,
            tool_name='quota-skill',
            tool_type='skill',
            risk_level='restricted',
            cost_class='free',
            allowlist_passed=True,
            confirmation_required=False,
            confirmation_passed=True,
            quota_passed=True,
            status='success',
            payload_keys=[],
            details={},
            created_at=datetime.now(),
        ))
        db.commit()

        decision = service.evaluate_execution(
            db=db,
            tool_name='quota-skill',
            payload={},
            agent_class='tasked',
            user_id=str(regular_user.id),
        )

        assert decision.passed is False
        assert decision.reason == 'quota_daily_calls_exceeded'

    def test_group_daily_quota_blocks_after_limit(self, db, regular_user):
        service = ToolPolicyService()
        target_group = AccessGroup(code='quota_group_daily', name='Quota Group Daily', enabled=True)
        skill = SkillEntry(
            name='quota-group-daily-skill',
            description='quota group daily',
            enabled=True,
            type='python',
            execution_policy={
                'risk_level': 'restricted',
                'rate_limit_profile': {
                    'per_group_daily_calls': 1,
                },
            },
        )
        db.add_all([target_group, skill])
        db.flush()
        db.add(UserGroupBinding(user_id=regular_user.id, group_id=target_group.id))
        db.add(ToolExecutionAudit(
            user_id=regular_user.id,
            tool_name='quota-group-daily-skill',
            tool_type='skill',
            risk_level='restricted',
            cost_class='free',
            allowlist_passed=True,
            confirmation_required=False,
            confirmation_passed=True,
            quota_passed=True,
            status='success',
            payload_keys=[],
            details={},
            created_at=datetime.now(),
        ))
        db.commit()

        decision = service.evaluate_execution(
            db=db,
            tool_name='quota-group-daily-skill',
            payload={},
            agent_class='tasked',
            user_id=str(regular_user.id),
        )

        assert decision.passed is False
        assert decision.reason == 'quota_group_daily_calls_exceeded'

    def test_monthly_cost_quota_blocks_after_limit(self, db, regular_user):
        service = ToolPolicyService()
        skill = SkillEntry(
            name='quota-monthly-cost-skill',
            description='quota monthly cost',
            enabled=True,
            type='python',
            execution_policy={
                'risk_level': 'restricted',
                'cost_class': 'billable',
                'rate_limit_profile': {
                    'monthly_cost_usd': 10,
                },
            },
        )
        db.add(skill)
        db.flush()
        db.add(ToolExecutionAudit(
            user_id=regular_user.id,
            tool_name='quota-monthly-cost-skill',
            tool_type='skill',
            risk_level='restricted',
            cost_class='billable',
            allowlist_passed=True,
            confirmation_required=False,
            confirmation_passed=True,
            quota_passed=True,
            status='success',
            payload_keys=[],
            cost_estimate=10,
            details={},
            created_at=datetime.now(),
        ))
        db.commit()

        decision = service.evaluate_execution(
            db=db,
            tool_name='quota-monthly-cost-skill',
            payload={},
            agent_class='tasked',
            user_id=str(regular_user.id),
        )

        assert decision.passed is False
        assert decision.reason == 'quota_monthly_cost_exceeded'

    def test_group_monthly_quota_blocks_after_limit(self, db, regular_user):
        service = ToolPolicyService()
        target_group = AccessGroup(code='quota_group_monthly', name='Quota Group Monthly', enabled=True)
        skill = SkillEntry(
            name='quota-group-monthly-skill',
            description='quota group monthly',
            enabled=True,
            type='python',
            execution_policy={
                'risk_level': 'restricted',
                'rate_limit_profile': {
                    'per_group_monthly_calls': 1,
                },
            },
        )
        db.add_all([target_group, skill])
        db.flush()
        db.add(UserGroupBinding(user_id=regular_user.id, group_id=target_group.id))
        db.add(ToolExecutionAudit(
            user_id=regular_user.id,
            tool_name='quota-group-monthly-skill',
            tool_type='skill',
            risk_level='restricted',
            cost_class='free',
            allowlist_passed=True,
            confirmation_required=False,
            confirmation_passed=True,
            quota_passed=True,
            status='success',
            payload_keys=[],
            details={},
            created_at=datetime.now(),
        ))
        db.commit()

        decision = service.evaluate_execution(
            db=db,
            tool_name='quota-group-monthly-skill',
            payload={},
            agent_class='tasked',
            user_id=str(regular_user.id),
        )

        assert decision.passed is False
        assert decision.reason == 'quota_group_monthly_calls_exceeded'
