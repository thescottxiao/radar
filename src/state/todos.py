"""Data access layer for Todos.

Todos are tasks with their own deadline/lifecycle — distinct from events (calendar)
and prep tasks (event-specific checklists). They can be standalone or linked to events.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.state.events import compute_title_similarity
from src.state.models import Todo, TodoChild, TodoStatus, TodoType


# ── Default reminder lead times by todo type ─────────────────────────────
# Used when the LLM doesn't provide a suggested_reminder_days hint.

TODO_REMINDER_DEFAULTS: dict[TodoType, int] = {
    TodoType.rsvp_needed: 1,           # Quick reply, 1 day before
    TodoType.form_to_sign: 2,          # Print/sign/return, 2 days
    TodoType.payment_due: 3,           # Budget + processing, 3 days
    TodoType.item_to_purchase: 3,      # Shopping trip, 3 days
    TodoType.item_to_bring: 1,         # Just remember, 1 day (morning of)
    TodoType.registration_deadline: 3,  # Forms + decisions, 3 days
    TodoType.contact_needed: 2,        # Find time to call, 2 days
    TodoType.other: 1,                 # Safe default
}


def get_reminder_days(todo_type: TodoType, llm_suggestion: int | None = None) -> int:
    """Resolve reminder lead time: LLM hint overrides type default."""
    if llm_suggestion is not None and llm_suggestion >= 0:
        return llm_suggestion
    return TODO_REMINDER_DEFAULTS.get(todo_type, 1)


# ── CRUD ─────────────────────────────────────────────────────────────────


async def create_todo(session: AsyncSession, family_id: UUID, **kwargs) -> Todo:
    todo = Todo(family_id=family_id, **kwargs)
    session.add(todo)
    await session.flush()
    return todo


async def get_todo(session: AsyncSession, family_id: UUID, todo_id: UUID) -> Todo | None:
    result = await session.execute(
        select(Todo).where(Todo.family_id == family_id, Todo.id == todo_id)
        .options(selectinload(Todo.children))
    )
    return result.scalar_one_or_none()


async def complete_todo(session: AsyncSession, family_id: UUID, todo_id: UUID) -> Todo:
    todo = await get_todo(session, family_id, todo_id)
    if todo is None:
        raise ValueError(f"Todo {todo_id} not found for family {family_id}")
    todo.status = TodoStatus.complete
    todo.completed_at = datetime.now(UTC)
    await session.flush()
    return todo


async def dismiss_todo(session: AsyncSession, family_id: UUID, todo_id: UUID) -> Todo:
    todo = await get_todo(session, family_id, todo_id)
    if todo is None:
        raise ValueError(f"Todo {todo_id} not found for family {family_id}")
    todo.status = TodoStatus.dismissed
    await session.flush()
    return todo


# ── Queries ──────────────────────────────────────────────────────────────


async def get_pending_todos(
    session: AsyncSession, family_id: UUID
) -> list[Todo]:
    result = await session.execute(
        select(Todo).where(
            Todo.family_id == family_id,
            Todo.status == TodoStatus.pending,
        ).order_by(Todo.due_date.asc().nullslast())
    )
    return list(result.scalars().all())


async def get_todos_for_family(
    session: AsyncSession, family_id: UUID, status: TodoStatus | None = None
) -> list[Todo]:
    stmt = select(Todo).where(Todo.family_id == family_id)
    if status is not None:
        stmt = stmt.where(Todo.status == status)
    stmt = stmt.order_by(Todo.due_date.asc().nullslast())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_todos_due_soon(
    session: AsyncSession, family_id: UUID, within_hours: int = 48,
    family_timezone: str | None = None,
) -> list[Todo]:
    if family_timezone:
        from src.utils.timezone import get_family_now
        now = get_family_now(family_timezone)
    else:
        now = datetime.now(UTC)
    cutoff = now + timedelta(hours=within_hours)
    result = await session.execute(
        select(Todo).where(
            Todo.family_id == family_id,
            Todo.status == TodoStatus.pending,
            Todo.due_date.is_not(None),
            Todo.due_date <= cutoff,
        ).order_by(Todo.due_date)
    )
    return list(result.scalars().all())


async def get_todos_for_event(
    session: AsyncSession, family_id: UUID, event_id: UUID,
    include_completed: bool = False,
) -> list[Todo]:
    """Get todos linked to a specific event.

    By default returns only pending todos. Pass include_completed=True
    to also return completed todos (used for GCal description rendering).
    """
    conditions = [
        Todo.family_id == family_id,
        Todo.event_id == event_id,
    ]
    if not include_completed:
        conditions.append(Todo.status == TodoStatus.pending)
    result = await session.execute(
        select(Todo).where(*conditions).order_by(Todo.due_date.asc().nullslast())
    )
    return list(result.scalars().all())


async def get_todos_needing_reminder(
    session: AsyncSession, family_id: UUID,
    family_timezone: str | None = None,
) -> list[Todo]:
    """Get todos that are within their reminder window but haven't been nudged yet.

    A todo needs a reminder when:
    - status is pending
    - has a due_date and reminder_days_before set
    - reminder_sent_at is NULL (not yet nudged)
    - now >= due_date - reminder_days_before (within the reminder window)
    - due_date > now (not yet past due — no point reminding after deadline)
    """
    if family_timezone:
        from src.utils.timezone import get_family_now
        now = get_family_now(family_timezone)
    else:
        now = datetime.now(UTC)

    # Fetch candidates and filter in Python for the interval check
    # (SQLAlchemy doesn't natively support column * INTERVAL arithmetic)
    result = await session.execute(
        select(Todo).where(
            Todo.family_id == family_id,
            Todo.status == TodoStatus.pending,
            Todo.due_date.is_not(None),
            Todo.reminder_days_before.is_not(None),
            Todo.reminder_sent_at.is_(None),
            Todo.due_date > now,  # not past due
        ).order_by(Todo.due_date)
    )
    candidates = list(result.scalars().all())

    # Filter: only those within their reminder window
    return [
        t for t in candidates
        if t.due_date - timedelta(days=t.reminder_days_before) <= now
    ]


async def mark_reminder_sent(session: AsyncSession, family_id: UUID, todo_id: UUID) -> None:
    """Mark a todo's standalone reminder as sent."""
    result = await session.execute(
        select(Todo).where(Todo.family_id == family_id, Todo.id == todo_id)
    )
    todo = result.scalar_one_or_none()
    if todo:
        todo.reminder_sent_at = datetime.now(UTC)
        await session.flush()


# ── Fuzzy matching ───────────────────────────────────────────────────────


async def find_todo_by_description(
    session: AsyncSession, family_id: UUID, description: str,
    similarity_threshold: float = 0.5,
) -> Todo | None:
    """Find a pending todo by fuzzy-matching against description.

    Used for WhatsApp "mark X done" flows where the user describes
    the todo in natural language.
    """
    pending = await get_pending_todos(session, family_id)
    best_match: Todo | None = None
    best_score = 0.0

    for todo in pending:
        score = compute_title_similarity(description, todo.description)
        if score > best_score and score >= similarity_threshold:
            best_score = score
            best_match = todo

    return best_match


# ── Child linking ────────────────────────────────────────────────────────


async def link_children_to_todo(
    session: AsyncSession, family_id: UUID, todo_id: UUID, child_ids: list[UUID]
) -> None:
    for child_id in child_ids:
        link = TodoChild(todo_id=todo_id, child_id=child_id, family_id=family_id)
        session.add(link)
    await session.flush()
