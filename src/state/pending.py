from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.state.models import PendingAction, PendingActionStatus, PendingActionType


async def expire_all_pending(session: AsyncSession) -> int:
    """Expire all awaiting_approval actions. Used on server restart to clear stale state."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(PendingAction)
        .where(PendingAction.status == PendingActionStatus.awaiting_approval)
        .values(status=PendingActionStatus.expired, resolved_at=now)
    )
    await session.flush()
    return result.rowcount


async def create_pending_action(
    session: AsyncSession,
    family_id: UUID,
    action_type: PendingActionType,
    draft_content: str,
    context: dict | None = None,
    initiated_by: UUID | None = None,
    expires_at: datetime | None = None,
) -> PendingAction:
    action = PendingAction(
        family_id=family_id,
        type=action_type,
        draft_content=draft_content,
        context=context or {},
        initiated_by=initiated_by,
        expires_at=expires_at,
    )
    session.add(action)
    await session.flush()
    return action


async def get_pending_action(
    session: AsyncSession, family_id: UUID, action_id: UUID
) -> PendingAction | None:
    """Fetch a single pending action by ID (filtered by family_id)."""
    result = await session.execute(
        select(PendingAction).where(
            PendingAction.family_id == family_id,
            PendingAction.id == action_id,
        )
    )
    return result.scalar_one_or_none()


async def get_active_pending(
    session: AsyncSession, family_id: UUID
) -> list[PendingAction]:
    """Get pending actions that are awaiting approval and not expired.

    Actions are considered expired if:
    - expires_at is set and in the past, OR
    - expires_at is NULL and created_at is older than 24 hours (legacy fallback)
    """
    now = datetime.now(UTC)
    fallback_cutoff = now - timedelta(hours=24)
    result = await session.execute(
        select(PendingAction).where(
            PendingAction.family_id == family_id,
            PendingAction.status == PendingActionStatus.awaiting_approval,
            or_(
                PendingAction.expires_at > now,
                # Legacy actions without expires_at: use created_at as fallback
                and_(
                    PendingAction.expires_at.is_(None),
                    PendingAction.created_at > fallback_cutoff,
                ),
            ),
        ).order_by(PendingAction.created_at.desc())
    )
    return list(result.scalars().all())


async def resolve_pending(
    session: AsyncSession,
    family_id: UUID,
    action_id: UUID,
    status: PendingActionStatus,
    resolved_by: UUID,
) -> PendingAction:
    result = await session.execute(
        select(PendingAction).where(
            PendingAction.family_id == family_id,
            PendingAction.id == action_id,
        )
    )
    action = result.scalar_one_or_none()
    if action is None:
        raise ValueError(f"PendingAction {action_id} not found")
    action.status = status
    action.resolved_by = resolved_by
    action.resolved_at = datetime.now().astimezone()
    await session.flush()
    return action


async def update_draft(
    session: AsyncSession,
    family_id: UUID,
    action_id: UUID,
    new_draft: str,
    edit_instruction: str,
) -> PendingAction:
    result = await session.execute(
        select(PendingAction).where(
            PendingAction.family_id == family_id,
            PendingAction.id == action_id,
        )
    )
    action = result.scalar_one_or_none()
    if action is None:
        raise ValueError(f"PendingAction {action_id} not found")

    history_entry = {
        "instruction": edit_instruction,
        "previous_draft": action.draft_content,
        "timestamp": datetime.now().astimezone().isoformat(),
    }
    action.edit_history = (action.edit_history or []) + [history_entry]
    action.draft_content = new_draft
    await session.flush()
    return action
