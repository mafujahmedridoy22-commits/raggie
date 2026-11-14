# SPDX-License-Identifier: Apache-2.0

"""SCIM 2.0 provisioning. The IdP's SCIM client creates / deactivates /
deletes users (and groups) via a bearer token. Users map to the users table
(auth_provider='scim'); groups map to teams.

Tenant boundary: the token is bound to exactly one organization (SCIM_ORG_ID)
and every read and write is confined to it. SCIM never takes over an account it
does not manage: an email that already belongs to a local/SSO account, or to
another org, is a uniqueness conflict (409), and only SCIM-managed users can be
(re)activated, deactivated or deleted through it.
"""

from __future__ import annotations

import hmac
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from raggie_control_plane.api.deps import get_conn
from raggie_control_plane.api.errors import not_found
from raggie_control_plane.config import settings
from raggie_control_plane.db.repositories import TeamRepo, UserRepo
from raggie_control_plane.models import User

router = APIRouter(prefix="/scim/v2", tags=["scim"])

_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
_SCIM_PROVIDER = "scim"

_bearer = HTTPBearer(auto_error=False)


def require_scim(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> UUID:
    """Authenticate the SCIM client and return the org its token is bound to.
    SCIM without a token or a tenant binding is treated as not enabled."""
    if not settings.scim_enabled or not settings.scim_bearer_token:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "scim is not enabled")
    try:
        org_id = UUID(settings.scim_org_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "scim is not enabled") from exc
    # Constant-time compare on bytes (str compare_digest rejects non-ASCII).
    if creds is None or not hmac.compare_digest(
        creds.credentials.encode(), settings.scim_bearer_token.encode()
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid scim token")
    return org_id


def _scim_error(status_code: int, scim_type: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "schemas": [_ERROR_SCHEMA],
            "status": str(status_code),
            "scimType": scim_type,
            "detail": detail,
        },
    )


def _is_scim_managed(user: User, org_id: UUID) -> bool:
    return user.auth_provider == _SCIM_PROVIDER and user.org_id == org_id


async def _org_user(conn: asyncpg.Connection, org_id: UUID, user_id: UUID) -> User:
    user = await UserRepo(conn).get(user_id)
    if user is None or user.org_id != org_id:
        raise not_found("user")
    return user


def _user_repr(u: User) -> dict:
    return {
        "schemas": [_USER_SCHEMA],
        "id": str(u.id),
        "userName": u.email,
        "externalId": u.external_id,
        "active": u.is_active,
        "emails": [{"value": u.email, "primary": True}],
        "meta": {"resourceType": "User"},
    }


class SCIMUserIn(BaseModel):
    userName: str
    externalId: str | None = None
    active: bool = True


class SCIMPatch(BaseModel):
    Operations: list[dict] = []


_NOT_MANAGED = "user is not managed by SCIM"


@router.post("/Users", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: SCIMUserIn,
    org_id: UUID = Depends(require_scim),
    conn: asyncpg.Connection = Depends(get_conn),
):
    repo = UserRepo(conn)
    external_id = body.externalId or body.userName
    holder = await repo.get_by_external_id(external_id)
    existing = await repo.get_by_email(body.userName)
    if existing is not None:
        # Re-provisioning a user SCIM already manages is idempotent; anything
        # else (local/SSO account, other org) is never taken over.
        if not _is_scim_managed(existing, org_id) or (
            holder is not None and holder.id != existing.id
        ):
            return _scim_error(409, "uniqueness", "userName already exists")
        user = existing
        if user.is_active != body.active:
            user = await repo.set_active(user.id, body.active) or user
        return _user_repr(user)
    if holder is not None:
        return _scim_error(409, "uniqueness", "externalId already exists")
    try:
        user = await repo.create_external(
            body.userName,
            "STAFF",
            org_id=org_id,
            auth_provider=_SCIM_PROVIDER,
            external_id=external_id,
        )
    except asyncpg.UniqueViolationError:
        return _scim_error(409, "uniqueness", "userName already exists")
    if not body.active:
        user = await repo.set_active(user.id, False) or user
    return _user_repr(user)


@router.get("/Users")
async def list_users(
    org_id: UUID = Depends(require_scim), conn: asyncpg.Connection = Depends(get_conn)
) -> dict:
    users = await UserRepo(conn).list(org_id=org_id, limit=200)
    return {
        "schemas": [_LIST_SCHEMA],
        "totalResults": len(users),
        "Resources": [_user_repr(u) for u in users],
    }


@router.get("/Users/{user_id}")
async def get_user(
    user_id: UUID,
    org_id: UUID = Depends(require_scim),
    conn: asyncpg.Connection = Depends(get_conn),
) -> dict:
    return _user_repr(await _org_user(conn, org_id, user_id))


@router.patch("/Users/{user_id}")
async def patch_user(
    user_id: UUID,
    body: SCIMPatch,
    org_id: UUID = Depends(require_scim),
    conn: asyncpg.Connection = Depends(get_conn),
):
    repo = UserRepo(conn)
    user = await _org_user(conn, org_id, user_id)
    if not _is_scim_managed(user, org_id):
        # e.g. an admin-deactivated local account must not be revived via SCIM.
        return _scim_error(409, "mutability", _NOT_MANAGED)
    # Honour the common Okta/Azure de/reactivation op: replace "active".
    for op in body.Operations:
        if str(op.get("op", "")).lower() == "replace":
            value = op.get("value", {})
            active = value.get("active") if isinstance(value, dict) else value
            if active is not None:
                user = await repo.set_active(user_id, bool(active)) or user
    return _user_repr(user)


@router.delete("/Users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: UUID,
    org_id: UUID = Depends(require_scim),
    conn: asyncpg.Connection = Depends(get_conn),
):
    user = await _org_user(conn, org_id, user_id)
    if not _is_scim_managed(user, org_id):
        return _scim_error(409, "mutability", _NOT_MANAGED)
    await UserRepo(conn).delete(user_id)
    return None


# ── Groups -> teams (create + list; Note: group member sync is not implemented) ─
class SCIMGroupIn(BaseModel):
    displayName: str


@router.post("/Groups", status_code=status.HTTP_201_CREATED)
async def create_group(
    body: SCIMGroupIn,
    org_id: UUID = Depends(require_scim),
    conn: asyncpg.Connection = Depends(get_conn),
) -> dict:
    team = await TeamRepo(conn).create(org_id, body.displayName)
    return {
        "schemas": [_GROUP_SCHEMA],
        "id": str(team.id),
        "displayName": team.name,
        "meta": {"resourceType": "Group"},
    }


@router.get("/Groups")
async def list_groups(
    org_id: UUID = Depends(require_scim), conn: asyncpg.Connection = Depends(get_conn)
) -> dict:
    teams = await TeamRepo(conn).list_by_org(org_id)
    return {
        "schemas": [_LIST_SCHEMA],
        "totalResults": len(teams),
        "Resources": [
            {"schemas": [_GROUP_SCHEMA], "id": str(t.id), "displayName": t.name} for t in teams
        ],
    }
