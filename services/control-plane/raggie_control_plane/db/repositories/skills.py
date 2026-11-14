# SPDX-License-Identifier: Apache-2.0

"""Skills and their versions."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from raggie_control_plane.db.repositories._common import compose_sql, patch_set
from raggie_control_plane.models import Skill, SkillVersion


class SkillsRepo:
    """Versioned assistant execution policies resolved by the edge at runtime."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def list_for_org(self, org_id: UUID, *, enabled_only: bool = False) -> list[Skill]:
        """Org-published catalog (owner_user_id IS NULL) — the admin builder set."""
        if enabled_only:
            rows = await self.conn.fetch(
                "SELECT * FROM skills WHERE org_id = $1 AND owner_user_id IS NULL"
                " AND enabled ORDER BY lower(name)",
                org_id,
            )
        else:
            rows = await self.conn.fetch(
                "SELECT * FROM skills WHERE org_id = $1 AND owner_user_id IS NULL"
                " ORDER BY lower(name)",
                org_id,
            )
        return [Skill(**dict(r)) for r in rows]

    async def list_effective(self, org_id: UUID, user_id: UUID) -> list[Skill]:
        """The chat picker's set: ENABLED org-published skills UNION the caller's
        OWN enabled personal skills. Org first, then personal, each by name."""
        rows = await self.conn.fetch(
            "SELECT * FROM skills WHERE org_id = $1 AND enabled AND"
            " ((owner_user_id IS NULL AND visibility = 'org')"
            "  OR owner_user_id = $2)"
            " ORDER BY (owner_user_id IS NOT NULL), lower(name)",
            org_id,
            user_id,
        )
        return [Skill(**dict(r)) for r in rows]

    async def list_personal(self, org_id: UUID, user_id: UUID) -> list[Skill]:
        """The caller's own personal skills (any enabled state) — their mgmt UI."""
        rows = await self.conn.fetch(
            "SELECT * FROM skills WHERE org_id = $1 AND owner_user_id = $2 ORDER BY lower(name)",
            org_id,
            user_id,
        )
        return [Skill(**dict(r)) for r in rows]

    async def get(self, skill_id: UUID) -> Skill | None:
        row = await self.conn.fetchrow("SELECT * FROM skills WHERE id = $1", skill_id)
        return Skill(**dict(row)) if row else None

    async def create(
        self,
        org_id: UUID,
        *,
        name: str,
        slug: str,
        instructions: str,
        description: str | None = None,
        icon: str | None = None,
        model: str | None = None,
        answer_style: str | None = None,
        allowed_tools: list[str] | None = None,
        enabled: bool = True,
        created_by: UUID | None = None,
        owner_user_id: UUID | None = None,
        visibility: str = "org",
    ) -> Skill:
        row = await self.conn.fetchrow(
            "INSERT INTO skills"
            " (org_id, created_by, owner_user_id, visibility, name, slug, description,"
            "  instructions, icon, model, answer_style, allowed_tools, enabled, updated_by)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $2)"
            " RETURNING *",
            org_id,
            created_by,
            owner_user_id,
            visibility,
            name,
            slug,
            description,
            instructions,
            icon,
            model,
            answer_style,
            list(allowed_tools or []),
            enabled,
        )
        return Skill(**dict(row))

    async def create_personal(
        self,
        org_id: UUID,
        user_id: UUID,
        *,
        name: str,
        slug: str,
        instructions: str,
        description: str | None = None,
        icon: str | None = None,
        model: str | None = None,
        answer_style: str | None = None,
        allowed_tools: list[str] | None = None,
        enabled: bool = True,
    ) -> Skill:
        """Create a skill OWNED by the caller — forces owner_user_id + personal."""
        return await self.create(
            org_id,
            name=name,
            slug=slug,
            instructions=instructions,
            description=description,
            icon=icon,
            model=model,
            answer_style=answer_style,
            allowed_tools=allowed_tools,
            enabled=enabled,
            created_by=user_id,
            owner_user_id=user_id,
            visibility="personal",
        )

    # Columns update() may patch. A dynamic SET (not COALESCE) lets an admin clear
    # a nullable pin (description/icon/model/answer_style) by passing it as None.
    _UPDATABLE = (
        "name",
        "slug",
        "description",
        "instructions",
        "icon",
        "model",
        "answer_style",
        "allowed_tools",
        "enabled",
    )

    async def update(
        self, skill_id: UUID, *, actor_id: UUID | None = None, **fields: object
    ) -> Skill | None:
        """Patch only the supplied columns (a key present is written verbatim,
        including None to clear a nullable field; absent keys are untouched)."""
        sets, vals = patch_set(self._UPDATABLE, fields, [skill_id])
        if not sets:
            return await self.get(skill_id)
        if actor_id is not None:
            vals.append(actor_id)
            sets.append(f"updated_by = ${len(vals)}")
        row = await self.conn.fetchrow(
            compose_sql(
                "UPDATE skills SET {sets}, updated_at = now() WHERE id = $1 RETURNING *",
                sets=sets,
            ),
            *vals,
        )
        return Skill(**dict(row)) if row else None

    async def update_personal(
        self, skill_id: UUID, user_id: UUID, **fields: object
    ) -> Skill | None:
        """Ownership-checked patch: only the owner may edit. Returns None if the
        skill is missing or not owned by user_id (so callers map that to 404)."""
        sets, vals = patch_set(self._UPDATABLE, fields, [skill_id, user_id])
        if not sets:
            row = await self.conn.fetchrow(
                "SELECT * FROM skills WHERE id = $1 AND owner_user_id = $2",
                skill_id,
                user_id,
            )
            return Skill(**dict(row)) if row else None
        vals.append(user_id)
        sets.append(f"updated_by = ${len(vals)}")
        row = await self.conn.fetchrow(
            compose_sql(
                "UPDATE skills SET {sets}, updated_at = now()"
                " WHERE id = $1 AND owner_user_id = $2 RETURNING *",
                sets=sets,
            ),
            *vals,
        )
        return Skill(**dict(row)) if row else None

    async def delete(self, skill_id: UUID, *, actor_id: UUID | None = None) -> bool:
        result = await self.conn.execute(
            "DELETE FROM skills WHERE id = $1",
            skill_id,
        )
        return result.endswith("1")

    async def delete_personal(self, skill_id: UUID, user_id: UUID) -> bool:
        """Ownership-checked delete: false if not the caller's own skill."""
        result = await self.conn.execute(
            "DELETE FROM skills WHERE id = $1 AND owner_user_id = $2",
            skill_id,
            user_id,
        )
        return result.endswith("1")

    async def list_versions(self, skill_id: UUID) -> list[SkillVersion]:
        rows = await self.conn.fetch(
            "SELECT * FROM skill_versions WHERE skill_id = $1 ORDER BY version DESC",
            skill_id,
        )
        return [SkillVersion(**dict(r)) for r in rows]

    async def restore_version(
        self, skill_id: UUID, version: int, *, actor_id: UUID
    ) -> Skill | None:
        snapshot = await self.conn.fetchrow(
            "SELECT instructions, model, answer_style, allowed_tools"
            " FROM skill_versions WHERE skill_id = $1 AND version = $2",
            skill_id,
            version,
        )
        if snapshot is None:
            return None
        return await self.update(
            skill_id,
            actor_id=actor_id,
            instructions=snapshot["instructions"],
            model=snapshot["model"],
            answer_style=snapshot["answer_style"],
            allowed_tools=snapshot["allowed_tools"],
        )
