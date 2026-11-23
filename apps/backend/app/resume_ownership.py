"""Request-scoped ownership for engine resume rows, including legacy callers.

An unscoped/local caller sees only legacy rows (owner_id IS NULL). A mobile
request enters resume_owner() with an identity verified by Supabase Auth.
Capture that scope on session creation, never on a shared engine or factory.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from app.models import (
    Application,
    Improvement,
    Job,
    MobileAccountDeletion,
    MobileEditorRevision,
    QueueItem,
    Resume,
    TailoringPreview,
)

_owner: ContextVar[str | None] = ContextVar("resume_owner", default=None)


def current_resume_owner() -> str | None:
    """Return the verified scope used to select the private engine store."""
    return _owner.get()


@contextmanager
def resume_owner(user_id: str) -> Iterator[None]:
    """Bind only a verified, canonical Supabase user ID to this async context."""
    token = _owner.set(str(UUID(user_id)))
    try:
        yield
    finally:
        _owner.reset(token)


class ResumeOwnerSession(Session):
    """ORM session whose resume ownership cannot change during its lifetime."""


class ResumeOwnerAsyncSession(AsyncSession):
    """Capture the caller's owner when each async session is constructed."""

    sync_session_class = ResumeOwnerSession

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        info = dict(kwargs.pop("info", None) or {})
        info["resume_owner"] = _owner.get()
        super().__init__(*args, info=info, **kwargs)


@event.listens_for(ResumeOwnerSession, "do_orm_execute")
def _scope_resume_queries(state: ORMExecuteState) -> None:
    """Apply the same owner predicate to ORM reads, updates and deletes."""
    if not state.is_orm_statement:
        return
    if state.is_insert or state.is_update or state.is_delete:
        _check_account_writable(state.session)
    if state.is_select or state.is_update or state.is_delete:
        owner = state.session.info.get("resume_owner")
        # PostgreSQL shares the private schema across owners and legacy tools.
        # Scope every customer entity, including session.get() identity reads.
        for model in (Resume, Job, TailoringPreview, Improvement, Application, QueueItem, MobileEditorRevision):
            criterion = model.owner_id.is_(None) if owner is None else model.owner_id == owner
            state.statement = state.statement.options(
                with_loader_criteria(model, criterion, include_aliases=True)
            )


@event.listens_for(ResumeOwnerSession, "before_flush")
def _protect_resume_writes(session: Session, *_args: Any) -> None:
    """Stamp inserts and reject ownership reassignment on normal ORM writes."""
    owner = session.info.get("resume_owner")
    _check_account_writable(session)
    for row in session.new:
        if isinstance(
            row, (Resume, Job, TailoringPreview, Improvement, Application, QueueItem)
        ):
            if row.owner_id not in (None, owner):
                raise ValueError("Cannot assign another resume owner")
            row.owner_id = owner
    for row in set(session.dirty) | set(session.deleted):
        if isinstance(
            row, (Resume, Job, TailoringPreview, Improvement, Application, QueueItem)
        ) and row.owner_id not in (None, owner):
            raise ValueError("Cannot change resume ownership")


def _check_account_writable(session: Session) -> None:
    """Check within the writer transaction, including operations already in flight."""
    owner = session.info.get("resume_owner")
    if owner and not session.info.get("account_deletion"):
        deleting = (
            session.connection()
            .execute(
                select(MobileAccountDeletion.owner_id).where(
                    MobileAccountDeletion.owner_id == owner,
                )
            )
            .first()
        )
        if deleting:
            raise ValueError("Account deletion is in progress")
