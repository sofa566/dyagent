from __future__ import annotations

from datetime import datetime

from src.models import SkillEntry, ToolExecutionAudit
from src.services.tool_policy_service import ToolPolicyService


class TestToolPolicyService:
    def test_dangerous_tool_requires_confirmation(self, db, regular_user):
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

        passed_decision = service.evaluate_execution(
            db=db,
            tool_name='dangerous-skill',
            payload={'_policy_confirmed': True},
            agent_class='tasked',
            user_id=str(regular_user.id),
        )
        assert passed_decision.passed is True
        assert passed_decision.reason == 'pass'

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
