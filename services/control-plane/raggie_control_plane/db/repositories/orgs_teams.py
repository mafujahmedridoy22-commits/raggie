# SPDX-License-Identifier: Apache-2.0

"""Organizations, departments, locations, teams and team membership."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from raggie_control_plane.db.repositories._common import compose_sql, patch_set
from raggie_control_plane.models import (
    Department,
    Location,
    Organization,
    Team,
)


class OrganizationRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def create(
        self,
        name: str,
        *,
        monthly_token_quota: int | None = None,
    ) -> Organization:
        row = await self.conn.fetchrow(
            "INSERT INTO organizations (name, monthly_token_quota) VALUES ($1, $2) RETURNING *",
            name,
            monthly_token_quota,
        )
        return Organization(**dict(row))

    async def get(self, org_id: UUID) -> Organization | None:
        row = await self.conn.fetchrow("SELECT * FROM organizations WHERE id = $1", org_id)
        return Organization(**dict(row)) if row else None

    async def list(self) -> list[Organization]:
        rows = await self.conn.fetch("SELECT * FROM organizations ORDER BY created_at")
        return [Organization(**dict(r)) for r in rows]


class DepartmentRepo:
    """Managed departments (0009). Org-scoped; an optional owner_user_id is the
    lead (display-only, no RBAC). Names are unique per org (case-insensitive)."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def list_by_org(self, org_id: UUID) -> list[Department]:
        rows = await self.conn.fetch(
            "SELECT * FROM departments WHERE org_id = $1 ORDER BY lower(name)", org_id
        )
        return [Department(**dict(r)) for r in rows]

    async def get(self, dept_id: UUID) -> Department | None:
        row = await self.conn.fetchrow("SELECT * FROM departments WHERE id = $1", dept_id)
        return Department(**dict(row)) if row else None

    async def create(
        self,
        org_id: UUID,
        name: str,
        *,
        owner_user_id: UUID | None = None,
        parent_department_id: UUID | None = None,
    ) -> Department:
        row = await self.conn.fetchrow(
            "INSERT INTO departments (org_id, name, owner_user_id, parent_department_id)"
            " VALUES ($1, $2, $3, $4) RETURNING *",
            org_id,
            name,
            owner_user_id,
            parent_department_id,
        )
        return Department(**dict(row))

    _UPDATABLE = ("name", "owner_user_id", "parent_department_id")

    async def update(self, dept_id: UUID, **fields: object) -> Department | None:
        sets, vals = patch_set(self._UPDATABLE, fields, [dept_id])
        if not sets:
            return await self.get(dept_id)
        row = await self.conn.fetchrow(
            compose_sql("UPDATE departments SET {sets} WHERE id = $1 RETURNING *", sets=sets),
            *vals,
        )
        return Department(**dict(row)) if row else None

    async def delete(self, dept_id: UUID) -> bool:
        result = await self.conn.execute("DELETE FROM departments WHERE id = $1", dept_id)
        return result.endswith("1")


class LocationRepo:
    """Managed office locations (0009). Org-scoped; unique name per org."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def list_by_org(self, org_id: UUID) -> list[Location]:
        rows = await self.conn.fetch(
            "SELECT * FROM locations WHERE org_id = $1 ORDER BY lower(name)", org_id
        )
        return [Location(**dict(r)) for r in rows]

    async def get(self, loc_id: UUID) -> Location | None:
        row = await self.conn.fetchrow("SELECT * FROM locations WHERE id = $1", loc_id)
        return Location(**dict(row)) if row else None

    async def create(
        self,
        org_id: UUID,
        name: str,
        *,
        city: str | None = None,
        country: str | None = None,
    ) -> Location:
        row = await self.conn.fetchrow(
            "INSERT INTO locations (org_id, name, city, country)"
            " VALUES ($1, $2, $3, $4) RETURNING *",
            org_id,
            name,
            city,
            country,
        )
        return Location(**dict(row))

    _UPDATABLE = ("name", "city", "country")

    async def update(self, loc_id: UUID, **fields: object) -> Location | None:
        sets, vals = patch_set(self._UPDATABLE, fields, [loc_id])
        if not sets:
            return await self.get(loc_id)
        row = await self.conn.fetchrow(
            compose_sql("UPDATE locations SET {sets} WHERE id = $1 RETURNING *", sets=sets),
            *vals,
        )
        return Location(**dict(row)) if row else None

    async def delete(self, loc_id: UUID) -> bool:
        result = await self.conn.execute("DELETE FROM locations WHERE id = $1", loc_id)
        return result.endswith("1")


class TeamRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def create(self, org_id: UUID, name: str, parent_team_id: UUID | None = None) -> Team:
        async with self.conn.transaction():
            row = await self.conn.fetchrow(
                "INSERT INTO teams (org_id, name, parent_team_id) VALUES ($1, $2, $3) RETURNING *",
                org_id,
                name,
                parent_team_id,
            )
            new_id = row["id"]
            if parent_team_id is None:
                await self.conn.execute(
                    "INSERT INTO team_closure (ancestor_id, descendant_id, depth)"
                    " VALUES ($1::uuid, $1::uuid, 0)",
                    new_id,
                )
            else:
                # New node inherits every ancestor of its parent (depth + 1), plus self.
                # Explicit ::uuid casts resolve UNION parameter-type inference.
                await self.conn.execute(
                    "INSERT INTO team_closure (ancestor_id, descendant_id, depth)"
                    " SELECT ancestor_id, $1::uuid, depth + 1 FROM team_closure"
                    "   WHERE descendant_id = $2::uuid"
                    " UNION ALL SELECT $1::uuid, $1::uuid, 0",
                    new_id,
                    parent_team_id,
                )
            return Team(**dict(row))

    async def get(self, team_id: UUID) -> Team | None:
        row = await self.conn.fetchrow("SELECT * FROM teams WHERE id = $1", team_id)
        return Team(**dict(row)) if row else None

    async def list_by_org(self, org_id: UUID) -> list[Team]:
        rows = await self.conn.fetch(
            "SELECT * FROM teams WHERE org_id = $1 ORDER BY created_at", org_id
        )
        return [Team(**dict(r)) for r in rows]

    async def descendant_ids(self, team_id: UUID) -> list[UUID]:
        """All teams at or below team_id (inclusive), via the closure table."""
        rows = await self.conn.fetch(
            "SELECT descendant_id FROM team_closure WHERE ancestor_id = $1", team_id
        )
        return [r["descendant_id"] for r in rows]


class MembershipRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def add_workspace_member(self, workspace_id: UUID, user_id: UUID, role: str) -> None:
        await self.conn.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role)"
            " VALUES ($1, $2, $3)"
            " ON CONFLICT (workspace_id, user_id) DO UPDATE SET role = EXCLUDED.role",
            workspace_id,
            user_id,
            role,
        )

    async def add_team_member(self, team_id: UUID, user_id: UUID, role: str) -> None:
        await self.conn.execute(
            "INSERT INTO team_members (team_id, user_id, role)"
            " VALUES ($1, $2, $3)"
            " ON CONFLICT (team_id, user_id) DO UPDATE SET role = EXCLUDED.role",
            team_id,
            user_id,
            role,
        )

    async def list_team_members(self, team_id: UUID) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT u.id, u.email, tm.role, u.is_active"
            " FROM team_members tm JOIN users u ON u.id = tm.user_id"
            " WHERE tm.team_id = $1 ORDER BY u.email",
            team_id,
        )
        return [dict(r) for r in rows]

    async def list_workspace_members(self, workspace_id: UUID) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT u.id, u.email, wm.role, u.is_active"
            " FROM workspace_members wm JOIN users u ON u.id = wm.user_id"
            " WHERE wm.workspace_id = $1 ORDER BY u.email",
            workspace_id,
        )
        return [dict(r) for r in rows]

    async def remove_team_member(self, team_id: UUID, user_id: UUID) -> bool:
        result = await self.conn.execute(
            "DELETE FROM team_members WHERE team_id = $1 AND user_id = $2", team_id, user_id
        )
        return result.endswith("1")

    async def remove_workspace_member(self, workspace_id: UUID, user_id: UUID) -> bool:
        result = await self.conn.execute(
            "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
            workspace_id,
            user_id,
        )
        return result.endswith("1")

    async def effective_team_ids(self, user_id: UUID) -> list[UUID]:
        """Teams a user acts in: every team they belong to and each team below
        one of those (the closure includes the team itself)."""
        rows = await self.conn.fetch(
            "SELECT DISTINCT tc.descendant_id FROM team_closure tc"
            " JOIN team_members tm ON tm.team_id = tc.ancestor_id"
            " WHERE tm.user_id = $1",
            user_id,
        )
        return [r["descendant_id"] for r in rows]

    async def effective_workspace_ids(self, user_id: UUID) -> list[UUID]:
        """Workspaces a user can access: direct membership OR membership in any
        team that is an ancestor of the workspace's team (inherited access)."""
        rows = await self.conn.fetch(
            "SELECT id FROM workspaces w WHERE"
            "  w.id IN (SELECT workspace_id FROM workspace_members WHERE user_id = $1)"
            "  OR w.team_id IN ("
            "     SELECT tc.descendant_id FROM team_closure tc"
            "     JOIN team_members tm ON tm.team_id = tc.ancestor_id"
            "     WHERE tm.user_id = $1)",
            user_id,
        )
        return [r["id"] for r in rows]
