# SPDX-License-Identifier: Apache-2.0

"""Scheduled tasks and the notifications inbox."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg

from raggie_control_plane.db.repositories._common import compose_sql, patch_set
from raggie_control_plane.models import Notification, ScheduledTask
from raggie_control_plane.scheduling import effective_timezone


class ScheduledTaskRepo:
    """Cron jobs (0014). Owned by a user, scoped to one of their workspaces. The
    control plane owns the catalog + CRUD; a separate scheduler WORKER reads
    next_run_at and executes. next_run_at is computed at the API layer (croniter)
    and passed in — this repo persists whatever it is given."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def list_for_user(self, user_id: UUID) -> list[ScheduledTask]:
        rows = await self.conn.fetch(
            "SELECT * FROM scheduled_tasks WHERE user_id = $1 ORDER BY created_at DESC",
            user_id,
        )
        return [ScheduledTask(**dict(r)) for r in rows]

    async def get(self, task_id: UUID) -> ScheduledTask | None:
        row = await self.conn.fetchrow("SELECT * FROM scheduled_tasks WHERE id = $1", task_id)
        return ScheduledTask(**dict(row)) if row else None

    async def create(
        self,
        org_id: UUID,
        user_id: UUID,
        workspace_id: UUID,
        *,
        name: str,
        prompt: str,
        cron: str,
        timezone: str | None = None,
        enabled: bool = True,
        web_search: bool = False,
        deep_search: bool = False,
        model: str | None = None,
        answer_style: str | None = None,
        next_run_at: datetime | None = None,
    ) -> ScheduledTask:
        row = await self.conn.fetchrow(
            "INSERT INTO scheduled_tasks"
            " (org_id, user_id, workspace_id, name, prompt, cron, timezone, enabled,"
            "  web_search, deep_search, model, answer_style, next_run_at)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)"
            " RETURNING *",
            org_id,
            user_id,
            workspace_id,
            name,
            prompt,
            cron,
            effective_timezone(timezone),
            enabled,
            web_search,
            deep_search,
            model,
            answer_style,
            next_run_at,
        )
        return ScheduledTask(**dict(row))

    # Columns update() may patch. A dynamic SET (not COALESCE) lets the owner clear
    # a nullable pin (model/answer_style) by passing it as None. next_run_at is
    # included so the API can recompute it when cron/timezone change.
    _UPDATABLE = (
        "name",
        "prompt",
        "cron",
        "timezone",
        "enabled",
        "web_search",
        "deep_search",
        "model",
        "answer_style",
        "next_run_at",
    )

    async def update(self, task_id: UUID, user_id: UUID, **fields: object) -> ScheduledTask | None:
        """Ownership-checked patch: only the owner may edit. Patches only the
        supplied columns (a key present is written verbatim, including None to
        clear a nullable field). Returns None if the task is missing or not owned
        by user_id (callers map that to 404)."""
        sets, vals = patch_set(self._UPDATABLE, fields, [task_id, user_id])
        if not sets:
            return await self._get_owned(task_id, user_id)
        row = await self.conn.fetchrow(
            compose_sql(
                "UPDATE scheduled_tasks SET {sets}, updated_at = now()"
                " WHERE id = $1 AND user_id = $2 RETURNING *",
                sets=sets,
            ),
            *vals,
        )
        return ScheduledTask(**dict(row)) if row else None

    async def _get_owned(self, task_id: UUID, user_id: UUID) -> ScheduledTask | None:
        row = await self.conn.fetchrow(
            "SELECT * FROM scheduled_tasks WHERE id = $1 AND user_id = $2",
            task_id,
            user_id,
        )
        return ScheduledTask(**dict(row)) if row else None

    async def delete(self, task_id: UUID, user_id: UUID) -> bool:
        """Ownership-checked delete: false if not the caller's own task."""
        result = await self.conn.execute(
            "DELETE FROM scheduled_tasks WHERE id = $1 AND user_id = $2",
            task_id,
            user_id,
        )
        return result.endswith("1")


class NotificationRepo:
    """The in-app inbox (0014). The scheduler WORKER writes rows directly; here a
    user lists / marks / deletes their OWN notifications (every method is scoped
    to user_id so one user can never touch another's inbox)."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def list_for_user(self, user_id: UUID, *, limit: int = 50) -> list[Notification]:
        rows = await self.conn.fetch(
            "SELECT * FROM notifications WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
            user_id,
            limit,
        )
        return [Notification(**dict(r)) for r in rows]

    async def unread_count(self, user_id: UUID) -> int:
        row = await self.conn.fetchrow(
            "SELECT count(*) AS n FROM notifications WHERE user_id = $1 AND NOT is_read",
            user_id,
        )
        return int(row["n"]) if row else 0

    async def mark_read(self, notification_id: UUID, user_id: UUID) -> bool:
        """Ownership-checked: false if missing or not the caller's. Already-read
        rows still return true (idempotent)."""
        result = await self.conn.execute(
            "UPDATE notifications SET is_read = true WHERE id = $1 AND user_id = $2",
            notification_id,
            user_id,
        )
        return result.endswith("1")

    async def mark_all_read(self, user_id: UUID) -> int:
        """Mark every unread notification of the user read; returns how many were
        flipped."""
        result = await self.conn.execute(
            "UPDATE notifications SET is_read = true WHERE user_id = $1 AND NOT is_read",
            user_id,
        )
        try:
            return int(result.split()[-1])  # asyncpg returns e.g. "UPDATE 3"
        except (ValueError, IndexError):
            return 0

    async def delete(self, notification_id: UUID, user_id: UUID) -> bool:
        """Ownership-checked delete: false if not the caller's own notification."""
        result = await self.conn.execute(
            "DELETE FROM notifications WHERE id = $1 AND user_id = $2",
            notification_id,
            user_id,
        )
        return result.endswith("1")
