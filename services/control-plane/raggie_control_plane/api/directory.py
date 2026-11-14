# SPDX-License-Identifier: Apache-2.0

"""Managed org directory: Departments (with a lead/owner) + Locations (offices).

These turn the previously free-text user profile fields into selectable, managed
entities. Org-scoped, guarded by MANAGE_ORG (no new RBAC action — an owner/lead is
display-only metadata). The user's department/location TEXT columns are still
written (write-through, in admin.py) because the Go edge reads them for the chat
persona; these endpoints own the catalog the UI selects from.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from raggie_control_plane.api.deps import get_conn, require_org
from raggie_control_plane.api.errors import not_found
from raggie_control_plane.authz import Action
from raggie_control_plane.db.repositories import (
    DepartmentRepo,
    LocationRepo,
    UserRepo,
)
from raggie_control_plane.models import Department, Location

router = APIRouter(prefix="/api/v1/admin", tags=["directory"])


# ── Departments ──────────────────────────────────────────────
class DepartmentIn(BaseModel):
    name: str
    owner_user_id: UUID | None = None
    parent_department_id: UUID | None = None


class DepartmentPatch(BaseModel):
    name: str | None = None
    owner_user_id: UUID | None = None
    parent_department_id: UUID | None = None


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split()).strip()
    if not cleaned:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name required")
    return cleaned


async def _check_owner_in_org(conn: asyncpg.Connection, owner_id: UUID, org_id: UUID) -> None:
    # Every id in a body is re-checked against the org in the path; a foreign
    # one is indistinguishable from a missing one (404).
    owner = await UserRepo(conn).get(owner_id)
    if owner is None or owner.org_id != org_id:
        raise not_found("owner")


async def _check_parent_in_org(
    conn: asyncpg.Connection, parent_id: UUID, org_id: UUID, dept_id: UUID | None = None
) -> None:
    """The parent must be an in-org department, and re-parenting must not close
    a cycle (A -> B -> A), which would make the department tree unrenderable."""
    repo = DepartmentRepo(conn)
    parent = await repo.get(parent_id)
    if parent is None or parent.org_id != org_id:
        raise not_found("parent department")
    if dept_id is None:
        return
    seen: set[UUID] = set()
    current: Department | None = parent
    while current is not None:
        if current.id == dept_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "department cycle not allowed")
        if current.id in seen or current.parent_department_id is None:
            break
        seen.add(current.id)
        current = await repo.get(current.parent_department_id)


@router.get(
    "/orgs/{org_id}/departments",
    response_model=list[Department],
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def list_departments(
    org_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> list[Department]:
    return await DepartmentRepo(conn).list_by_org(org_id)


@router.post(
    "/orgs/{org_id}/departments",
    response_model=Department,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def create_department(
    org_id: UUID, body: DepartmentIn, conn: asyncpg.Connection = Depends(get_conn)
) -> Department:
    name = _clean_name(body.name)
    if body.owner_user_id is not None:
        await _check_owner_in_org(conn, body.owner_user_id, org_id)
    if body.parent_department_id is not None:
        await _check_parent_in_org(conn, body.parent_department_id, org_id)
    try:
        return await DepartmentRepo(conn).create(
            org_id,
            name,
            owner_user_id=body.owner_user_id,
            parent_department_id=body.parent_department_id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "department already exists") from exc


@router.patch(
    "/orgs/{org_id}/departments/{dept_id}",
    response_model=Department,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def update_department(
    org_id: UUID,
    dept_id: UUID,
    body: DepartmentPatch,
    conn: asyncpg.Connection = Depends(get_conn),
) -> Department:
    repo = DepartmentRepo(conn)
    dept = await repo.get(dept_id)
    if dept is None or dept.org_id != org_id:
        raise not_found("department")
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"] is not None:
        fields["name"] = _clean_name(fields["name"])
    if fields.get("owner_user_id") is not None:
        await _check_owner_in_org(conn, fields["owner_user_id"], org_id)
    if fields.get("parent_department_id") == dept_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "department cannot be its own parent")
    if fields.get("parent_department_id") is not None:
        await _check_parent_in_org(conn, fields["parent_department_id"], org_id, dept_id)
    try:
        updated = await repo.update(dept_id, **fields)
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "department already exists") from exc
    if updated is None:
        raise not_found("department")
    return updated


@router.delete(
    "/orgs/{org_id}/departments/{dept_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def delete_department(
    org_id: UUID, dept_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> None:
    repo = DepartmentRepo(conn)
    dept = await repo.get(dept_id)
    if dept is None or dept.org_id != org_id:
        raise not_found("department")
    # FKs are ON DELETE SET NULL, so members simply become unassigned.
    await repo.delete(dept_id)


# ── Locations ────────────────────────────────────────────────
class LocationIn(BaseModel):
    name: str
    city: str | None = None
    country: str | None = None


class LocationPatch(BaseModel):
    name: str | None = None
    city: str | None = None
    country: str | None = None


@router.get(
    "/orgs/{org_id}/locations",
    response_model=list[Location],
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def list_locations(
    org_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> list[Location]:
    return await LocationRepo(conn).list_by_org(org_id)


@router.post(
    "/orgs/{org_id}/locations",
    response_model=Location,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def create_location(
    org_id: UUID, body: LocationIn, conn: asyncpg.Connection = Depends(get_conn)
) -> Location:
    name = _clean_name(body.name)
    try:
        return await LocationRepo(conn).create(org_id, name, city=body.city, country=body.country)
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "location already exists") from exc


@router.patch(
    "/orgs/{org_id}/locations/{loc_id}",
    response_model=Location,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def update_location(
    org_id: UUID,
    loc_id: UUID,
    body: LocationPatch,
    conn: asyncpg.Connection = Depends(get_conn),
) -> Location:
    repo = LocationRepo(conn)
    loc = await repo.get(loc_id)
    if loc is None or loc.org_id != org_id:
        raise not_found("location")
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"] is not None:
        fields["name"] = _clean_name(fields["name"])
    try:
        updated = await repo.update(loc_id, **fields)
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "location already exists") from exc
    if updated is None:
        raise not_found("location")
    return updated


@router.delete(
    "/orgs/{org_id}/locations/{loc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def delete_location(
    org_id: UUID, loc_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> None:
    repo = LocationRepo(conn)
    loc = await repo.get(loc_id)
    if loc is None or loc.org_id != org_id:
        raise not_found("location")
    await repo.delete(loc_id)


# ── Org chart (manager-based reporting tree, read-only) ───────
class OrgChartNode(BaseModel):
    id: UUID
    name: str  # full_name or email
    email: str
    job_title: str | None = None
    department: str | None = None
    manager_user_id: UUID | None = None


@router.get(
    "/orgs/{org_id}/org-chart",
    response_model=list[OrgChartNode],
    dependencies=[Depends(require_org(Action.MANAGE_ORG))],
)
async def org_chart(
    org_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> list[OrgChartNode]:
    """Flat list of org members with their manager link; the web builds the tree
    client-side (bucketing null-manager users under a synthetic root and breaking
    any cycles with a visited-set)."""
    users = await UserRepo(conn).list(org_id=org_id, limit=1000)
    return [
        OrgChartNode(
            id=u.id,
            name=u.full_name or u.email,
            email=u.email,
            job_title=u.job_title,
            department=u.department,
            manager_user_id=u.manager_user_id,
        )
        for u in users
    ]
