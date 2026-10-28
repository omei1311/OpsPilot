"""Offline approval tests use a temporary SQLite file, never application services."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db.base import Base  # noqa: E402
from app.models.agent import AgentConversation, AgentPendingAction, PendingActionStatus  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402


@pytest.fixture
def sessions(tmp_path):
    """Independent connections make the concurrent tests exercise the database CAS."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'approvals.sqlite').as_posix()}",
        poolclass=NullPool,
        connect_args={"timeout": 10},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def seed():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            for user_id, role, active in (
                (1, UserRole.OPERATOR, True),
                (2, UserRole.OPERATOR, True),
                (3, UserRole.ADMIN, True),
                (4, UserRole.OPERATOR, False),
            ):
                session.add(
                    User(
                        id=user_id,
                        username=f"test-{user_id}",
                        email=f"test-{user_id}@example.invalid",
                        password_hash="test-only-placeholder",
                        role=role,
                        is_active=active,
                    )
                )
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            session.add(AgentConversation(id=1, user_id=1, title="test", last_active_at=now))
            session.add(
                AgentPendingAction(
                    id=1,
                    action_id="approval-1",
                    run_id="run-1",
                    conversation_id=1,
                    requested_by=1,
                    title="Update three tickets",
                    risk_level="high",
                    payload={
                        "tool": "batch_update_tickets",
                        "args": {"ticket_ids": [101, 102, 103], "priority": "urgent"},
                    },
                    preview=[{"id": ticket_id} for ticket_id in (101, 102, 103)],
                    affected_count=3,
                    status=PendingActionStatus.PENDING,
                    expires_at=now + timedelta(hours=1),
                )
            )
            await session.commit()

    asyncio.run(seed())
    yield factory
    asyncio.run(engine.dispose())
