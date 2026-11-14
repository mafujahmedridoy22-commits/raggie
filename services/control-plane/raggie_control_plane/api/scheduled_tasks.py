# SPDX-License-Identifier: Apache-2.0

"""Scheduled tasks (cron jobs) + the in-app notifications inbox (0014).

A scheduled task is a prompt the assistant runs on a cron schedule for its owner.
A separate scheduler WORKER (not the control plane) polls enabled tasks by
next_run_at, calls the edge to execute them, and writes a notification row. This
router owns the user-facing CRUD only: every endpoint is scoped to the current
user, and next_run_at is computed here (croniter) on create and whenever the
cron/timezone change.

The notifications endpoints let a user read / mark / delete their OWN inbox; the
worker populates it directly via SQL.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from raggie_control_plane.api.deps import get_conn, get_current_user
from raggie_control_plane.api.errors import not_found
from raggie_control_plane.authz import Action, Authorizer
from raggie_control_plane.db.repositories import (
    NotificationRepo,
    ScheduledTaskRepo,
    SettingsRepo,
    WorkspaceRepo,
)
from raggie_control_plane.models import Notification, ScheduledTask, User
from raggie_control_plane.scheduling import InvalidSchedule, effective_timezone, next_run

router = APIRouter(prefix="/api/v1", tags=["scheduled-tasks"])

_ALLOWED_STYLES = {"corporate", "short", "instructive", "balanced"}


class ScheduledTaskIn(BaseModel):
    name: str
    workspace_id: UUID
    prompt: str
    cron: str
    timezone: str = Field(default_factory=lambda: effective_timezone(None))
    web_search: bool = False
    deep_search: bool = False
    model: str | None = None
    answer_style: str | None = None
    enabled: bool = True


class ScheduledTaskPatch(BaseModel):
    name: str | None = None
    workspace_id: UUID | None = None
    prompt: str | None = None
    cron: str | None = None
    timezone: str | None = None
    web_search: bool | None = None
    deep_search: bool | None = None
    model: str | None = None
    answer_style: str | None = None
    enabled: bool | None = None


class NotificationsOut(BaseModel):
    items: list[Notification]
    unread: int


def _clean_required(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{field} required")
    return cleaned


def _validate_answer_style(style: str | None) -> str | None:
    """Normalize a pinned answer_style. Empty string clears the pin (None); a
    provided value must be one of the allowed styles."""
    if style is None:
        return None
    cleaned = style.strip()
    if not cleaned:
        return None
    if cleaned not in _ALLOWED_STYLES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"answer_style must be one of {sorted(_ALLOWED_STYLES)}",
        )
    return cleaned


async def _validate_model(conn: asyncpg.Connection, model: str | None) -> str | None:
    """Normalize a pinned canonical model id and enforce the platform allow-list."""
    if model is None:
        return None
    cleaned = model.strip()
    if not cleaned:
        return None
    config = await SettingsRepo(conn).get_ai_config()
    if cleaned not in config["chat_allowed_models"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "model is not allowed")
    return cleaned


async def _require_org_workspace(conn: asyncpg.Connection, user: User, workspace_id: UUID) -> None:
    """The task's workspace must belong to the caller's org AND the caller must
    be allowed to chat in it (same rule as the chat surface). A workspace in
    another org is indistinguishable from a missing one (404); an in-org
    workspace the caller is not a member of is forbidden (403) — the worker
    would otherwise run prompts against documents the owner may not read."""
    if user.org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no organization in scope")
    ws = await WorkspaceRepo(conn).get(workspace_id)
    if ws is None or ws.org_id != user.org_id:
        raise not_found("workspace")
    if not await Authorizer(conn).resolve(
        user, Action.USE_WORKSPACE_CHAT, workspace_id=workspace_id
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")


def _compute_next_run(cron: str, tz: str) -> object:
    try:
        return next_run(cron, tz)
    except InvalidSchedule as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


# ── Scheduled tasks: current-user CRUD ───────────────────────────
@router.get("/scheduled-tasks", response_model=list[ScheduledTask])
async def list_scheduled_tasks(
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[ScheduledTask]:
    """The caller's own scheduled tasks, newest first."""
    return await ScheduledTaskRepo(conn).list_for_user(user.id)


@router.post(
    "/scheduled-tasks",
    response_model=ScheduledTask,
    status_code=status.HTTP_201_CREATED,
)
async def create_scheduled_task(
    body: ScheduledTaskIn,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> ScheduledTask:
    if user.org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no organization in scope")
    name = _clean_required(body.name, "name")
    prompt = _clean_required(body.prompt, "prompt")
    answer_style = _validate_answer_style(body.answer_style)
    model = await _validate_model(conn, body.model)
    await _require_org_workspace(conn, user, body.workspace_id)
    upcoming = _compute_next_run(body.cron, body.timezone)
    return await ScheduledTaskRepo(conn).create(
        user.org_id,
        user.id,
        body.workspace_id,
        name=name,
        prompt=prompt,
        cron=body.cron.strip(),
        timezone=effective_timezone(body.timezone),
        enabled=body.enabled,
        web_search=body.web_search,
        deep_search=body.deep_search,
        model=model,
        answer_style=answer_style,
        next_run_at=upcoming,
    )


@router.put("/scheduled-tasks/{task_id}", response_model=ScheduledTask)
async def update_scheduled_task(
    task_id: UUID,
    body: ScheduledTaskPatch,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> ScheduledTask:
    repo = ScheduledTaskRepo(conn)
    existing = await repo.get(task_id)
    # A task that is missing or owned by someone else is invisible (404).
    if existing is None or existing.user_id != user.id:
        raise not_found("task")

    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"] is not None:
        fields["name"] = _clean_required(fields["name"], "name")
    if "prompt" in fields and fields["prompt"] is not None:
        fields["prompt"] = _clean_required(fields["prompt"], "prompt")
    if "answer_style" in fields:
        fields["answer_style"] = _validate_answer_style(fields["answer_style"])
    if "model" in fields:
        fields["model"] = await _validate_model(conn, fields["model"])
    if "workspace_id" in fields and fields["workspace_id"] is not None:
        await _require_org_workspace(conn, user, fields["workspace_id"])
    if "cron" in fields and fields["cron"] is not None:
        fields["cron"] = fields["cron"].strip()
    if "timezone" in fields and fields["timezone"] is not None:
        fields["timezone"] = effective_timezone(fields["timezone"])

    # Recompute next_run_at whenever the schedule (cron or timezone) changes, using
    # the would-be effective values (the patched field if present, else current).
    if "cron" in fields or "timezone" in fields:
        eff_cron = fields.get("cron", existing.cron)
        eff_tz = fields.get("timezone", existing.timezone)
        fields["next_run_at"] = _compute_next_run(eff_cron, eff_tz)

    updated = await repo.update(task_id, user.id, **fields)
    if updated is None:
        raise not_found("task")
    return updated


@router.delete(
    "/scheduled-tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_scheduled_task(
    task_id: UUID,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    if not await ScheduledTaskRepo(conn).delete(task_id, user.id):
        raise not_found("task")


# ── Notifications inbox: current-user only ───────────────────────
@router.get("/notifications", response_model=NotificationsOut)
async def list_notifications(
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> NotificationsOut:
    """The caller's latest ~50 notifications plus their unread count (the badge)."""
    repo = NotificationRepo(conn)
    items = await repo.list_for_user(user.id, limit=50)
    unread = await repo.unread_count(user.id)
    return NotificationsOut(items=items, unread=unread)


@router.post("/notifications/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_notification_read(
    notification_id: UUID,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    if not await NotificationRepo(conn).mark_read(notification_id, user.id):
        raise not_found("notification")


@router.post("/notifications/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_notifications_read(
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    await NotificationRepo(conn).mark_all_read(user.id)


@router.delete("/notifications/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification(
    notification_id: UUID,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    if not await NotificationRepo(conn).delete(notification_id, user.id):
        raise not_found("notification")
