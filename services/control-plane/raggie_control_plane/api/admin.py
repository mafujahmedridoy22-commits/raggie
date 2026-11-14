# SPDX-License-Identifier: Apache-2.0

"""Admin/management CRUD: organizations, teams (nested), workspaces, members.

All routes live under /api/v1/admin/* (the private admin surface, which widget
tokens can never reach) and are
guarded by the central Authorizer via route-level dependencies.
"""

from __future__ import annotations

import secrets
from datetime import date
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from raggie_control_plane.api.deps import (
    get_conn,
    get_current_user,
    require_org,
    require_system,
    require_workspace,
)
from raggie_control_plane.api.errors import not_found
from raggie_control_plane.authz import Action, can_grant_role, is_platform_admin
from raggie_control_plane.db.repositories import (
    AuditRepo,
    AuthSessionRepo,
    DepartmentRepo,
    DocumentRepo,
    LocationRepo,
    MembershipRepo,
    OrganizationRepo,
    TeamRepo,
    UserRepo,
    WorkspaceRepo,
)
from raggie_control_plane.models import (
    Document,
    Organization,
    Role,
    Team,
    User,
    Workspace,
    WorkspaceType,
)
from raggie_control_plane.security import NewPassword, hash_password

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# ── Organizations (platform super-admin) ─────────────────────
class OrgIn(BaseModel):
    # Unknown fields are ignored, so clients still sending the retired
    # embedding_dim (the embeddings gateway owns the dimension) keep working.
    name: str


@router.post(
    "/orgs",
    response_model=Organization,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_system())],
)
async def create_org(body: OrgIn, conn: asyncpg.Connection = Depends(get_conn)) -> Organization:
    return await OrganizationRepo(conn).create(body.name)


@router.get(
    "/orgs",
    response_model=list[Organization],
    dependencies=[Depends(require_system())],
)
async def list_orgs(conn: asyncpg.Connection = Depends(get_conn)) -> list[Organization]:
    return await OrganizationRepo(conn).list()


@router.get(
    "/orgs/{org_id}",
    response_model=Organization,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def get_org(org_id: UUID, conn: asyncpg.Connection = Depends(get_conn)) -> Organization:
    org = await OrganizationRepo(conn).get(org_id)
    if org is None:
        raise not_found("organization")
    return org


# ── Teams (nested) ───────────────────────────────────────────
class TeamIn(BaseModel):
    name: str
    parent_team_id: UUID | None = None


async def require_team_in_org(conn: asyncpg.Connection, org_id: UUID, team_id: UUID) -> Team:
    """Resolve a team that belongs to ``org_id``. The route guard only proves
    authority over the org in the path; every team id (path or body) must be
    re-checked against it, else a tenant could address another org's team.
    A foreign team is indistinguishable from a missing one (404)."""
    team = await TeamRepo(conn).get(team_id)
    if team is None or team.org_id != org_id:
        raise not_found("team")
    return team


async def _require_user_in_org(conn: asyncpg.Connection, org_id: UUID, user_id: UUID) -> User:
    user = await UserRepo(conn).get(user_id)
    if user is None or user.org_id != org_id:
        raise not_found("user")
    return user


@router.post(
    "/orgs/{org_id}/teams",
    response_model=Team,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def create_team(
    org_id: UUID, body: TeamIn, conn: asyncpg.Connection = Depends(get_conn)
) -> Team:
    if body.parent_team_id is not None:
        await require_team_in_org(conn, org_id, body.parent_team_id)
    return await TeamRepo(conn).create(org_id, body.name, body.parent_team_id)


@router.get(
    "/orgs/{org_id}/teams",
    response_model=list[Team],
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def list_teams(org_id: UUID, conn: asyncpg.Connection = Depends(get_conn)) -> list[Team]:
    return await TeamRepo(conn).list_by_org(org_id)


@router.get(
    "/orgs/{org_id}/teams/{team_id}",
    response_model=Team,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def get_team(
    org_id: UUID, team_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> Team:
    return await require_team_in_org(conn, org_id, team_id)


@router.get(
    "/orgs/{org_id}/teams/{team_id}/workspaces",
    response_model=list[Workspace],
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def list_team_workspaces(
    org_id: UUID, team_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> list[Workspace]:
    await require_team_in_org(conn, org_id, team_id)
    # Includes sub-team workspaces (closure descendants) — a parent's pool.
    team_ids = await TeamRepo(conn).descendant_ids(team_id)
    return await WorkspaceRepo(conn).list_by_team_ids(team_ids)


@router.get(
    "/orgs/{org_id}/teams/{team_id}/documents",
    response_model=list[Document],
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def list_team_documents(
    org_id: UUID, team_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> list[Document]:
    await require_team_in_org(conn, org_id, team_id)
    team_ids = await TeamRepo(conn).descendant_ids(team_id)
    return await DocumentRepo(conn).list_by_team_ids(team_ids)


# ── Users (admin account management) ─────────────────────────
_ROLE_VALUES = {r.value for r in Role}


class OrgUserOut(User):
    """A user row enriched with the resolved manager name + computed tenure."""

    manager_display: str | None = None
    tenure_days: int | None = None


class CreateUserIn(BaseModel):
    email: str
    role: Role = Role.STAFF
    password: NewPassword | None = None  # omit → a temp password is generated
    full_name: str | None = None
    job_title: str | None = None
    department: str | None = None
    department_id: UUID | None = None  # managed directory (write-through to TEXT)
    manager_user_id: UUID | None = None
    hire_date: date | None = None
    phone: str | None = None
    location: str | None = None
    location_id: UUID | None = None  # managed directory (write-through to TEXT)
    timezone: str | None = None
    locale: str | None = None


class CreateUserOut(BaseModel):
    user: User
    temp_password: str | None = None  # present only when generated; shown ONCE


class UpdateUserIn(BaseModel):
    email: str | None = None
    role: Role | None = None
    full_name: str | None = None
    job_title: str | None = None
    department: str | None = None
    department_id: UUID | None = None
    manager_user_id: UUID | None = None
    hire_date: date | None = None
    phone: str | None = None
    location: str | None = None
    location_id: UUID | None = None
    timezone: str | None = None
    locale: str | None = None


class SetActiveIn(BaseModel):
    active: bool


class ResetPasswordOut(BaseModel):
    temp_password: str


_CREATE_PROFILE_KEYS = {
    "full_name",
    "job_title",
    "department",
    "department_id",
    "manager_user_id",
    "hire_date",
    "phone",
    "location",
    "location_id",
    "timezone",
    "locale",
}


async def _apply_directory_write_through(
    conn: asyncpg.Connection, org_id: UUID, fields: dict
) -> None:
    """When an admin selects a managed department/location (by id), mirror its
    name into the free-text column the Go edge reads for the chat persona, and
    validate the entity belongs to this org. Mutates ``fields`` in place. Setting
    an id to None clears the id and (unless explicitly set) the text too."""
    if "department_id" in fields:
        dept_id = fields["department_id"]
        if dept_id is not None:
            dept = await DepartmentRepo(conn).get(dept_id)
            if dept is None or dept.org_id != org_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "department not in org")
            fields.setdefault("department", dept.name)
        else:
            fields.setdefault("department", None)
    if "location_id" in fields:
        loc_id = fields["location_id"]
        if loc_id is not None:
            loc = await LocationRepo(conn).get(loc_id)
            if loc is None or loc.org_id != org_id:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "location not in org")
            fields.setdefault("location", loc.name)
        else:
            fields.setdefault("location", None)


async def _would_create_manager_cycle(
    conn: asyncpg.Connection, user_id: UUID, manager_id: UUID
) -> bool:
    """Walk the proposed manager's reporting chain; a cycle exists if we reach
    user_id. Guards the multi-hop case the DB self-FK alone can't (A→B→A)."""
    seen: set[UUID] = set()
    current: UUID | None = manager_id
    while current is not None:
        if current == user_id:
            return True
        if current in seen:  # a pre-existing cycle elsewhere — stop, don't loop
            break
        seen.add(current)
        row = await conn.fetchrow("SELECT manager_user_id FROM users WHERE id = $1", current)
        current = row["manager_user_id"] if row else None
    return False


def _enrich_users(users: list[User]) -> list[OrgUserOut]:
    by_id = {u.id: u for u in users}
    out: list[OrgUserOut] = []
    for u in users:
        manager_display = None
        if u.manager_user_id is not None:
            mgr = by_id.get(u.manager_user_id)
            if mgr is not None:
                manager_display = mgr.full_name or mgr.email
        tenure_days = (date.today() - u.hire_date).days if u.hire_date else None
        out.append(
            OrgUserOut(**u.model_dump(), manager_display=manager_display, tenure_days=tenure_days)
        )
    return out


async def _load_target(conn: asyncpg.Connection, actor: User, org_id: UUID, user_id: UUID) -> User:
    """Resolve a target user in-org. Nobody may act on an account that outranks
    them: a WORKSPACE_ADMIN never on a SUPER_ADMIN, and only a platform admin on
    a platform admin (a reset password would otherwise hand over the platform)."""
    target = await _require_user_in_org(conn, org_id, user_id)
    if not can_grant_role(actor, target.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if is_platform_admin(target) and not is_platform_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    return target


@router.get(
    "/orgs/{org_id}/users",
    response_model=list[OrgUserOut],
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def list_org_users(
    org_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> list[OrgUserOut]:
    return _enrich_users(await UserRepo(conn).list(org_id=org_id, limit=500))


@router.post(
    "/orgs/{org_id}/users",
    response_model=CreateUserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def create_org_user(
    org_id: UUID,
    body: CreateUserIn,
    actor: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> CreateUserOut:
    if not can_grant_role(actor, body.role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"cannot grant {body.role}")
    if await UserRepo(conn).get_by_email(body.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "email already in use")
    # Same rule as update_org_user; a brand-new user has no reports, so no cycle.
    if body.manager_user_id is not None:
        mgr = await UserRepo(conn).get(body.manager_user_id)
        if mgr is None or mgr.org_id != org_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "manager not in organization")
    temp_password = body.password or secrets.token_urlsafe(12)
    profile = body.model_dump(include=_CREATE_PROFILE_KEYS, exclude_none=True)
    await _apply_directory_write_through(conn, org_id, profile)
    created = await UserRepo(conn).create(
        body.email,
        hash_password(temp_password),
        str(body.role),
        org_id=org_id,
        must_change_password=True,
        **profile,
    )
    await AuditRepo(conn).record(
        "user.created", org_id=org_id, actor_user_id=actor.id, target=str(created.id)
    )
    return CreateUserOut(user=created, temp_password=None if body.password else temp_password)


@router.patch(
    "/orgs/{org_id}/users/{user_id}",
    response_model=User,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def update_org_user(
    org_id: UUID,
    user_id: UUID,
    body: UpdateUserIn,
    actor: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> User:
    target = await _load_target(conn, actor, org_id, user_id)
    fields = body.model_dump(exclude_unset=True)
    if "role" in fields and fields["role"] is not None:
        role_value = str(fields["role"])
        fields["role"] = role_value
        if not can_grant_role(actor, role_value):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"cannot grant {role_value}")
    mgr_id = fields.get("manager_user_id")
    if mgr_id is not None:
        if mgr_id == target.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot be own manager")
        mgr = await UserRepo(conn).get(mgr_id)
        if mgr is None or mgr.org_id != org_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "manager not in organization")
        if await _would_create_manager_cycle(conn, target.id, mgr_id):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "manager cycle not allowed")
    await _apply_directory_write_through(conn, org_id, fields)
    updated = await UserRepo(conn).update_profile(user_id, **fields)
    if updated is None:
        raise not_found("user")
    await AuditRepo(conn).record(
        "user.updated", org_id=org_id, actor_user_id=actor.id, target=str(user_id)
    )
    return updated


@router.patch(
    "/orgs/{org_id}/users/{user_id}/active",
    response_model=User,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def set_org_user_active(
    org_id: UUID,
    user_id: UUID,
    body: SetActiveIn,
    actor: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> User:
    target = await _load_target(conn, actor, org_id, user_id)
    if target.id == actor.id and not body.active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot deactivate yourself")
    updated = await UserRepo(conn).set_active(user_id, body.active)
    if updated is None:
        raise not_found("user")
    await AuditRepo(conn).record(
        "user.activated" if body.active else "user.deactivated",
        org_id=org_id,
        actor_user_id=actor.id,
        target=str(user_id),
    )
    return updated


@router.post(
    "/orgs/{org_id}/users/{user_id}/reset-password",
    response_model=ResetPasswordOut,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def reset_org_user_password(
    org_id: UUID,
    user_id: UUID,
    actor: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> ResetPasswordOut:
    target = await _load_target(conn, actor, org_id, user_id)
    if target.auth_provider != "local":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "password managed by identity provider")
    temp_password = secrets.token_urlsafe(12)
    repo = UserRepo(conn)
    await repo.set_password_hash(user_id, hash_password(temp_password))
    await repo.set_must_change_password(user_id, True)
    await AuthSessionRepo(conn).revoke_all(user_id)
    await AuditRepo(conn).record(
        "user.password_reset", org_id=org_id, actor_user_id=actor.id, target=str(user_id)
    )
    return ResetPasswordOut(temp_password=temp_password)


# ── Platform admins (operator authority across every org) ────
class PlatformAdminIn(BaseModel):
    is_platform_admin: bool


@router.put(
    "/users/{user_id}/platform-admin",
    response_model=User,
    dependencies=[Depends(require_system())],
)
async def set_platform_admin(
    user_id: UUID,
    body: PlatformAdminIn,
    actor: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> User:
    """Grant / revoke users.is_platform_admin (platform admin only). Only an
    active SUPER_ADMIN can be granted; the last active platform admin can never
    be revoked (self included), so the platform is never left unmanageable."""
    repo = UserRepo(conn)
    async with conn.transaction():
        admins = await repo.lock_active_platform_admins()
        target = await repo.get(user_id)
        if target is None:
            raise not_found("user")
        if body.is_platform_admin:
            if not target.is_active or target.role != Role.SUPER_ADMIN:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "only an active SUPER_ADMIN can be a platform admin"
                )
        elif admins == [target.id]:
            raise HTTPException(status.HTTP_409_CONFLICT, "cannot revoke the last platform admin")
        updated = await repo.set_platform_admin(user_id, body.is_platform_admin)
        assert updated is not None
        await AuditRepo(conn).record(
            "user.platform_admin_granted"
            if body.is_platform_admin
            else "user.platform_admin_revoked",
            org_id=target.org_id,
            actor_user_id=actor.id,
            target=str(user_id),
        )
    return updated


# ── Workspaces ───────────────────────────────────────────────
class WorkspaceIn(BaseModel):
    name: str
    type: WorkspaceType
    team_id: UUID | None = None
    system_prompt: str = ""
    temperature: float = 0.3
    widget_primary_color: str = "#3498db"
    web_search_enabled: bool = False
    personalization_enabled: bool = False
    memory_enabled: bool = False


@router.post(
    "/orgs/{org_id}/workspaces",
    response_model=Workspace,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def create_workspace(
    org_id: UUID, body: WorkspaceIn, conn: asyncpg.Connection = Depends(get_conn)
) -> Workspace:
    if body.team_id is not None:
        await require_team_in_org(conn, org_id, body.team_id)
    return await WorkspaceRepo(conn).create(
        org_id,
        body.name,
        str(body.type),
        team_id=body.team_id,
        system_prompt=body.system_prompt,
        temperature=body.temperature,
        widget_primary_color=body.widget_primary_color,
        web_search_enabled=body.web_search_enabled,
        personalization_enabled=body.personalization_enabled,
        memory_enabled=body.memory_enabled,
    )


@router.get(
    "/orgs/{org_id}/workspaces",
    response_model=list[Workspace],
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def list_workspaces(
    org_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> list[Workspace]:
    return await WorkspaceRepo(conn).list_by_org(org_id)


@router.get(
    "/workspaces/{workspace_id}",
    response_model=Workspace,
    dependencies=[Depends(require_workspace(Action.VIEW_WORKSPACE))],
)
async def get_workspace(
    workspace_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> Workspace:
    ws = await WorkspaceRepo(conn).get(workspace_id)
    if ws is None:
        raise not_found("workspace")
    return ws


# ── Members ──────────────────────────────────────────────────
class MemberIn(BaseModel):
    user_id: UUID
    role: Role = Role.STAFF


def _check_member_role(actor: User, role: Role) -> None:
    # Membership roles follow the invite rule: never above the actor's own.
    if not can_grant_role(actor, role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"cannot grant {role}")


class MemberOut(BaseModel):
    id: UUID
    email: str
    role: Role
    is_active: bool = True


# Workspace members
@router.get(
    "/workspaces/{workspace_id}/members",
    response_model=list[MemberOut],
    dependencies=[Depends(require_workspace(Action.VIEW_WORKSPACE))],
)
async def list_workspace_members(
    workspace_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> list[MemberOut]:
    return [MemberOut(**m) for m in await MembershipRepo(conn).list_workspace_members(workspace_id)]


@router.post(
    "/workspaces/{workspace_id}/members",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def add_workspace_member(
    workspace_id: UUID,
    body: MemberIn,
    actor: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    _check_member_role(actor, body.role)
    ws = await WorkspaceRepo(conn).get(workspace_id)
    if ws is None:
        raise not_found("workspace")
    await _require_user_in_org(conn, ws.org_id, body.user_id)
    await MembershipRepo(conn).add_workspace_member(workspace_id, body.user_id, str(body.role))


@router.delete(
    "/workspaces/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def remove_workspace_member(
    workspace_id: UUID, user_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> None:
    if not await MembershipRepo(conn).remove_workspace_member(workspace_id, user_id):
        raise not_found("member")


# Team members
@router.get(
    "/orgs/{org_id}/teams/{team_id}/members",
    response_model=list[MemberOut],
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def list_team_members(
    org_id: UUID, team_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> list[MemberOut]:
    await require_team_in_org(conn, org_id, team_id)
    return [MemberOut(**m) for m in await MembershipRepo(conn).list_team_members(team_id)]


@router.post(
    "/orgs/{org_id}/teams/{team_id}/members",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def add_team_member(
    org_id: UUID,
    team_id: UUID,
    body: MemberIn,
    actor: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    _check_member_role(actor, body.role)
    await require_team_in_org(conn, org_id, team_id)
    await _require_user_in_org(conn, org_id, body.user_id)
    await MembershipRepo(conn).add_team_member(team_id, body.user_id, str(body.role))


@router.delete(
    "/orgs/{org_id}/teams/{team_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def remove_team_member(
    org_id: UUID, team_id: UUID, user_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> None:
    await require_team_in_org(conn, org_id, team_id)
    if not await MembershipRepo(conn).remove_team_member(team_id, user_id):
        raise not_found("member")
