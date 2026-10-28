"""Regression coverage for approval races, authorization, and preview scope."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.core.config import settings
from app.core.exceptions import BadRequestError, ConflictError, ForbiddenError
from app.models.agent import AgentPendingAction, PendingActionStatus
from app.models.user import User, UserRole
from app.services.agent_service import AgentService


async def load(session, user_id=1):
    action = await session.scalar(select(AgentPendingAction))
    user = await session.get(User, user_id)
    return action, user


def test_approve_subset_preserves_tool_and_business_arguments(sessions):
    async def scenario():
        async with sessions() as session:
            action, user = await load(session)
            result = await AgentService.decide_action(
                session,
                action,
                approve=True,
                user=user,
                edited_payload={"ticket_ids": [103, 101]},
                note="Only the selected tickets",
            )
            assert result.status == PendingActionStatus.APPROVED
            assert result.payload == {
                "tool": "batch_update_tickets",
                "args": {"ticket_ids": [103, 101], "priority": "urgent"},
            }
            assert result.affected_count == 2
            assert result.decided_by == 1
            assert result.decision_note == "Only the selected tickets"

    asyncio.run(scenario())


@pytest.mark.parametrize("approve", [True, False])
def test_unedited_approval_and_rejection_are_persisted(sessions, approve):
    async def scenario():
        async with sessions() as session:
            action, user = await load(session)
            result = await AgentService.decide_action(session, action, approve=approve, user=user)
            expected = PendingActionStatus.APPROVED if approve else PendingActionStatus.REJECTED
            assert result.status == expected
            assert result.payload["args"]["ticket_ids"] == [101, 102, 103]
        async with sessions() as session:
            stored, _ = await load(session)
            assert stored.status == expected

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "edited",
    [
        {},
        {"ticket_ids": []},
        {"ticket_ids": [101, 101]},
        {"ticket_ids": [999]},
        {"ticket_ids": [True]},
        {"ticket_ids": [101.0]},
        {"ticket_ids": ["101"]},
        {"ticket_ids": [-1]},
        {"ticket_ids": [0]},
        {"ticket_ids": None},
        {"ticket_ids": "101"},
        {"ticket_ids": [101], "priority": "low"},
        {"ticket_ids": [101], "tool": "batch_assign_tickets"},
        {"args": {"ticket_ids": [101]}},
    ],
)
def test_invalid_edits_do_not_change_the_plan_or_approve(sessions, edited):
    async def scenario():
        async with sessions() as session:
            action, user = await load(session)
            with pytest.raises(BadRequestError):
                await AgentService.decide_action(
                    session, action, approve=True, user=user, edited_payload=edited
                )
        async with sessions() as session:
            stored, _ = await load(session)
            assert stored.status == PendingActionStatus.PENDING
            assert stored.affected_count == 3
            assert stored.payload["args"]["ticket_ids"] == [101, 102, 103]
            assert stored.decided_by is None

    asyncio.run(scenario())


@pytest.mark.parametrize("original_ids", [[], [101, 101], [True], ["101"]])
def test_malformed_original_plan_requires_a_new_plan(sessions, original_ids):
    async def scenario():
        async with sessions() as session:
            action, user = await load(session)
            action.payload = {"tool": "batch_update_tickets", "args": {"ticket_ids": original_ids}}
            await session.commit()
            with pytest.raises(ConflictError):
                await AgentService.decide_action(session, action, approve=True, user=user)
            await session.refresh(action)
            assert action.status == PendingActionStatus.PENDING

    asyncio.run(scenario())


def test_current_batch_limit_is_enforced_but_a_smaller_selection_can_pass(sessions, monkeypatch):
    monkeypatch.setattr(settings, "AGENT_BATCH_LIMIT", 2)

    async def scenario():
        async with sessions() as session:
            action, user = await load(session)
            with pytest.raises(BadRequestError):
                await AgentService.decide_action(session, action, approve=True, user=user)
            result = await AgentService.decide_action(
                session, action, approve=True, user=user, edited_payload={"ticket_ids": [101, 102]}
            )
            assert result.affected_count == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("user_id", [2, 4])
def test_other_user_or_disabled_user_cannot_decide(sessions, user_id):
    async def scenario():
        async with sessions() as session:
            action, user = await load(session, user_id)
            with pytest.raises(ForbiddenError):
                await AgentService.decide_action(session, action, approve=True, user=user)
        async with sessions() as session:
            action, _ = await load(session)
            assert action.status == PendingActionStatus.PENDING

    asyncio.run(scenario())


def test_current_admin_can_approve_another_users_plan(sessions):
    async def scenario():
        async with sessions() as session:
            action, user = await load(session, 3)
            result = await AgentService.decide_action(session, action, approve=True, user=user)
            assert result.decided_by == 3

    asyncio.run(scenario())


def test_role_change_is_checked_even_with_a_cached_user_object(sessions):
    async def scenario():
        async with sessions() as session:
            action, admin = await load(session, 3)
            async with sessions() as other:
                await other.execute(update(User).where(User.id == 3).values(role=UserRole.USER))
                await other.commit()
            assert admin.role == UserRole.ADMIN  # The cached ORM object really is stale.
            with pytest.raises(ForbiddenError):
                await AgentService.decide_action(session, action, approve=True, user=admin)

    asyncio.run(scenario())


def test_permission_change_immediately_before_update_cannot_claim_approval(sessions, monkeypatch):
    async def scenario():
        async with sessions() as session:
            action, user = await load(session)
            original_execute = session.execute

            async def execute_with_disable(statement, *args, **kwargs):
                if getattr(statement, "is_update", False):
                    async with sessions() as other:
                        await other.execute(update(User).where(User.id == 1).values(is_active=False))
                        await other.commit()
                return await original_execute(statement, *args, **kwargs)

            monkeypatch.setattr(session, "execute", execute_with_disable)
            with pytest.raises(ConflictError):
                await AgentService.decide_action(session, action, approve=True, user=user)
        async with sessions() as session:
            action, _ = await load(session)
            assert action.status == PendingActionStatus.PENDING

    asyncio.run(scenario())


def test_expired_plan_becomes_expired_instead_of_approved(sessions):
    async def scenario():
        async with sessions() as session:
            action, user = await load(session)
            action.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
            await session.commit()
            with pytest.raises(ConflictError, match="超时"):
                await AgentService.decide_action(session, action, approve=True, user=user)
        async with sessions() as session:
            action, _ = await load(session)
            assert action.status == PendingActionStatus.EXPIRED
            assert action.decided_by is None

    asyncio.run(scenario())


def test_expiration_worker_winning_race_cannot_be_overwritten(sessions, monkeypatch):
    async def scenario():
        async with sessions() as session:
            action, user = await load(session)
            original_execute = session.execute

            async def execute_after_expiration(statement, *args, **kwargs):
                if getattr(statement, "is_update", False):
                    async with sessions() as other:
                        await other.execute(
                            update(AgentPendingAction).values(
                                expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
                                - timedelta(seconds=1)
                            )
                        )
                        await other.commit()
                        assert await AgentService.expire_stale_actions(other) == 1
                return await original_execute(statement, *args, **kwargs)

            monkeypatch.setattr(session, "execute", execute_after_expiration)
            with pytest.raises(ConflictError):
                await AgentService.decide_action(session, action, approve=True, user=user)
        async with sessions() as session:
            action, _ = await load(session)
            assert action.status == PendingActionStatus.EXPIRED

    asyncio.run(scenario())


@pytest.mark.parametrize("decisions", [(True, True), (True, False), (False, False)])
def test_two_independent_requests_have_exactly_one_decision_winner(sessions, decisions, monkeypatch):
    async def scenario():
        ready_to_write = asyncio.Event()
        arrived = 0

        async def request(approve):
            nonlocal arrived
            async with sessions() as session:
                action, user = await load(session)
                assert action.status == PendingActionStatus.PENDING
                original_execute = session.execute

                async def execute_together(statement, *args, **kwargs):
                    nonlocal arrived
                    if getattr(statement, "is_update", False):
                        # Both requests finish their fresh reads and validation before
                        # either UPDATE is sent: a Python-only status check cannot pass.
                        arrived += 1
                        if arrived == 2:
                            ready_to_write.set()
                        await ready_to_write.wait()
                    return await original_execute(statement, *args, **kwargs)

                monkeypatch.setattr(session, "execute", execute_together)
                try:
                    decided = await AgentService.decide_action(
                        session, action, approve=approve, user=user
                    )
                except ConflictError:
                    return "conflict"
                return decided.status

        results = await asyncio.wait_for(
            asyncio.gather(*(request(approve) for approve in decisions)), timeout=15
        )
        assert results.count("conflict") == 1
        winner = next(result for result in results if result != "conflict")
        async with sessions() as session:
            action, _ = await load(session)
            assert action.status == winner

    asyncio.run(scenario())


def test_executor_uses_fresh_role_after_approval(sessions, monkeypatch):
    from app.agent import executor

    seen_roles = []

    async def fake_write(db, tool_name, args, ticket_ids, user):
        seen_roles.append(user.role)
        return {"total": len(ticket_ids), "succeeded_count": len(ticket_ids), "failed_count": 0,
                "failed": []}

    monkeypatch.setattr(executor, "_execute_ticket_tool", fake_write)

    async def scenario():
        async with sessions() as session:
            await session.execute(update(User).where(User.id == 1).values(role=UserRole.ADMIN))
            await session.commit()
            action, user = await load(session)
            action = await AgentService.decide_action(session, action, approve=True, user=user)
            async with sessions() as other:
                await other.execute(update(User).where(User.id == 1).values(role=UserRole.USER))
                await other.commit()
            assert user.role == UserRole.ADMIN
            result = await executor.execute_action(session, action, user)
            assert result["ok"]
            assert seen_roles == [UserRole.USER]

    asyncio.run(scenario())


@pytest.mark.parametrize("user_id, change", [(1, {"is_active": False}), (3, {"role": "user"})])
def test_executor_stops_when_approver_loses_access(sessions, monkeypatch, user_id, change):
    from app.agent import executor

    async def unexpected_write(*args, **kwargs):
        pytest.fail("A disabled or no-longer-authorized approver must not reach business writes")

    monkeypatch.setattr(executor, "_execute_ticket_tool", unexpected_write)

    async def scenario():
        async with sessions() as session:
            action, user = await load(session, user_id)
            action = await AgentService.decide_action(session, action, approve=True, user=user)
            async with sessions() as other:
                await other.execute(update(User).where(User.id == user_id).values(**change))
                await other.commit()
            result = await executor.execute_action(session, action, user)
            assert result["ok"] is False
            assert action.status == PendingActionStatus.FAILED

    asyncio.run(scenario())


@pytest.mark.parametrize("approve", [True, False])
def test_later_request_cannot_overwrite_a_committed_decision(sessions, approve):
    async def scenario():
        async with sessions() as first, sessions() as second:
            stale_action, stale_user = await load(second)
            action, user = await load(first)
            await AgentService.decide_action(first, action, approve=approve, user=user)
            with pytest.raises(ConflictError):
                await AgentService.decide_action(
                    second, stale_action, approve=not approve, user=stale_user
                )

    asyncio.run(scenario())
