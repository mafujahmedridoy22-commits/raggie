# SPDX-License-Identifier: Apache-2.0

"""Authentication, account-settings & invite endpoints."""

from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, date, datetime
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from raggie_control_plane.api.deps import get_access_claims, get_conn, get_current_user
from raggie_control_plane.api.errors import not_found
from raggie_control_plane.api.ratelimit import auth_rate_limit, login_lockout
from raggie_control_plane.auth.identity import LocalIdentityProvider
from raggie_control_plane.auth.service import AuthService
from raggie_control_plane.authz import can_grant_role
from raggie_control_plane.config import SUPPORTED_LOCALES, settings
from raggie_control_plane.db.repositories import (
    AuditRepo,
    AuthSessionRepo,
    InviteRepo,
    SettingsRepo,
    TeamRepo,
    UserMemoryRepo,
    UserRepo,
    WorkspaceRepo,
)
from raggie_control_plane.models import Role, User
from raggie_control_plane.security import NewPassword, hash_password

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# Allowed per-user chat answer styles (0010). Kept in sync with the DB CHECK
# constraint in migration 0010 and the edge's persona assembly.
ANSWER_STYLES = ("corporate", "short", "instructive", "balanced")

# --- Memory import (paste a summary from another assistant) parsing limits ----
MEMORY_IMPORT_MAX_TEXT = 20_000  # reject the whole paste above this (chars)
MEMORY_IMPORT_MAX_FACT = 300  # cap a single fact (chars)
MEMORY_IMPORT_MIN_FACT = 3  # drop anything shorter than this (chars)
MEMORY_IMPORT_MAX_FACTS = 50  # cap how many facts a single import inserts

# Leading list/bullet markers to strip: "-", "*", "•", "·", "1.", "2)", "(3)".
_MEMORY_BULLET_RE = re.compile(r"^\s*(?:[-*•·]+|\(?\d+[.)])\s+")
# Leading markdown emphasis markers ("**", "__", "*", "_", "`").
_MEMORY_EMPHASIS_RE = re.compile(r"^[*_`]+|[*_`]+$")
# Section-header lines we drop: a short label ending in ":" with no other ":".
# Matches "Demografik Bilgiler:" / "1. Demografik Bilgiler:" / "Preferences:".
_MEMORY_HEADER_RE = re.compile(r"^[^:]{1,60}:\s*$")
# Evidence/citation lines (multilingual) and the import-provenance footer.
_MEMORY_DROP_PREFIXES = ("kanıt:", "kanit:", "evidence:", "kaynak:", "source:")
# Footer the web's (localized) memory-transfer prompt asks the other assistant
# to end with: "Şuradan içe aktarıldı: <name>" (tr) / "Imported from: <name>" (en).
_MEMORY_PROVENANCE_PREFIXES = ("şuradan içe aktarıldı", "suradan ice aktarildi", "imported from")


def _parse_memory_import(text: str) -> list[str]:
    """Turn a structured summary pasted from another AI assistant into atomic
    memory facts. Pure parsing (no LLM): split on newlines; strip bullet/list
    markers and markdown emphasis; drop blank lines, section headers,
    evidence/citation lines, and the trailing provenance line; trim, cap fact
    length, dedupe (case-insensitive, order-preserving) and cap the total."""
    facts: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = _MEMORY_BULLET_RE.sub("", raw_line).strip()
        line = _MEMORY_EMPHASIS_RE.sub("", line).strip()
        if len(line) < MEMORY_IMPORT_MIN_FACT:
            continue
        low = line.lower()
        if _MEMORY_HEADER_RE.match(line):
            continue
        if low.startswith(_MEMORY_DROP_PREFIXES):
            continue
        if low.startswith(_MEMORY_PROVENANCE_PREFIXES):
            continue
        line = line[:MEMORY_IMPORT_MAX_FACT].strip()
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        facts.append(line)
        if len(facts) >= MEMORY_IMPORT_MAX_FACTS:
            break
    return facts


class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshIn(BaseModel):
    refresh_token: str


@router.post("/login", response_model=TokenOut, dependencies=[Depends(auth_rate_limit)])
async def login(
    body: LoginIn, request: Request, conn: asyncpg.Connection = Depends(get_conn)
) -> TokenOut:
    lockout = login_lockout(request)
    now = time.time()
    # Checked before the password so a locked account can't keep being guessed.
    retry_after = await lockout.retry_after(body.email, now)
    if retry_after:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many failed sign-in attempts; try again later",
            headers={"Retry-After": str(retry_after)},
        )
    tokens = await AuthService(conn).login(body.email, body.password)
    if tokens is None:
        if await lockout.register_failure(body.email, now):
            await _audit(conn, "auth.account_locked", body.email, actor=None, org_id=None)
        await _audit(conn, "auth.login_failed", body.email, actor=None, org_id=None)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    await lockout.reset(body.email)
    user = await UserRepo(conn).get_by_email(body.email)
    await _audit(
        conn,
        "auth.login",
        body.email,
        actor=user.id if user else None,
        org_id=user.org_id if user else None,
    )
    return TokenOut(access_token=tokens.access_token, refresh_token=tokens.refresh_token)


@router.post(
    "/refresh",
    response_model=TokenOut,
    dependencies=[Depends(auth_rate_limit)],
)
async def refresh(body: RefreshIn, conn: asyncpg.Connection = Depends(get_conn)) -> TokenOut:
    tokens = await AuthService(conn).refresh(body.refresh_token)
    if tokens is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")
    return TokenOut(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    claims: dict = Depends(get_access_claims),
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    await AuthSessionRepo(conn).revoke(UUID(str(claims["sid"])), user.id)


async def _audit(conn: asyncpg.Connection, action: str, target: str, *, actor, org_id) -> None:
    try:
        await AuditRepo(conn).record(action, org_id=org_id, actor_user_id=actor, target=target)
    except Exception:  # noqa: BLE001 — auditing is best-effort, never block auth
        pass


@router.get("/me", response_model=User)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


class InviteIn(BaseModel):
    email: str
    role: Role = Role.STAFF
    team_id: UUID | None = None
    workspace_id: UUID | None = None
    # Optional admin-seeded profile carried by the invite.
    full_name: str | None = None
    job_title: str | None = None
    department: str | None = None
    manager_user_id: UUID | None = None
    phone: str | None = None
    location: str | None = None
    timezone: str | None = None
    locale: str | None = None


class InviteOut(BaseModel):
    invite_id: UUID
    token: str  # raw token, returned ONCE for the copy-link
    accept_path: str
    expires_at: str


_INVITE_PROFILE_KEYS = {
    "full_name",
    "job_title",
    "department",
    "manager_user_id",
    "phone",
    "location",
    "timezone",
    "locale",
}


class SelfProfileUpdate(BaseModel):
    """Fields a user may edit on their own account (not role/email/manager)."""

    full_name: str | None = None
    job_title: str | None = None
    department: str | None = None
    phone: str | None = None
    location: str | None = None
    timezone: str | None = None


class ProfileResponse(BaseModel):
    user: User
    manager_display: str | None = None
    tenure_days: int | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: NewPassword


class LocaleRequest(BaseModel):
    locale: str


class PreferencesUpdate(BaseModel):
    """Per-user chat preferences. Both optional; a field present (even as null,
    for preferred_chat_model) is written, absent fields are left untouched."""

    answer_style: str | None = None
    preferred_chat_model: str | None = None


class InviteListItem(BaseModel):
    id: UUID
    email: str
    role: str
    status: str  # pending | accepted | revoked | expired
    full_name: str | None = None
    department: str | None = None
    expires_at: datetime
    created_at: datetime


class InvitePreview(BaseModel):
    email: str
    role: str
    full_name: str | None = None
    job_title: str | None = None
    department: str | None = None
    phone: str | None = None
    location: str | None = None
    timezone: str | None = None
    locale: str | None = None


def _invite_status(invite) -> str:
    if invite.accepted_at is not None:
        return "accepted"
    if invite.revoked_at is not None:
        return "revoked"
    if invite.expires_at <= datetime.now(UTC):
        return "expired"
    return "pending"


async def _profile_response(conn: asyncpg.Connection, user: User) -> ProfileResponse:
    manager_display: str | None = None
    if user.manager_user_id is not None:
        mgr = await UserRepo(conn).get(user.manager_user_id)
        if mgr is not None:
            manager_display = mgr.full_name or mgr.email
    tenure_days = (date.today() - user.hire_date).days if user.hire_date else None
    return ProfileResponse(user=user, manager_display=manager_display, tenure_days=tenure_days)


async def _check_invite_grant(
    conn: asyncpg.Connection,
    inviter: User,
    role: Role | str,
    team_id: UUID | None,
    workspace_id: UUID | None,
    manager_user_id: UUID | None = None,
) -> None:
    """An invite may never carry a role above the inviter's own, and its team /
    workspace must belong to the inviter's org (accepting it grants membership),
    as must its manager (the invitee's profile shows the manager's name)."""
    if not can_grant_role(inviter, role):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"cannot invite {role}")
    if team_id is not None:
        team = await TeamRepo(conn).get(team_id)
        if team is None or team.org_id != inviter.org_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "team not in organization")
    if workspace_id is not None:
        ws = await WorkspaceRepo(conn).get(workspace_id)
        if ws is None or ws.org_id != inviter.org_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "workspace not in organization")
    if manager_user_id is not None:
        manager = await UserRepo(conn).get(manager_user_id)
        if manager is None or manager.org_id != inviter.org_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "manager not in organization")


@router.post("/invites", response_model=InviteOut, status_code=status.HTTP_201_CREATED)
async def create_invite(
    body: InviteIn,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> InviteOut:
    if user.role not in (Role.SUPER_ADMIN, Role.WORKSPACE_ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
    if user.org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "inviter has no organization")
    await _check_invite_grant(
        conn, user, body.role, body.team_id, body.workspace_id, body.manager_user_id
    )
    profile = body.model_dump(include=_INVITE_PROFILE_KEYS, exclude_none=True)
    invite, raw = await AuthService(conn).create_invite(
        user.org_id,
        body.email,
        str(body.role),
        created_by=user.id,
        team_id=body.team_id,
        workspace_id=body.workspace_id,
        **profile,
    )
    return InviteOut(
        invite_id=invite.id,
        token=raw,
        accept_path=f"/onboarding/accept?token={raw}",
        expires_at=invite.expires_at.isoformat(),
    )


# ---- Account settings (self-service) --------------------------------------


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> ProfileResponse:
    return await _profile_response(conn, user)


@router.patch("/profile", response_model=ProfileResponse)
async def update_my_profile(
    body: SelfProfileUpdate,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> ProfileResponse:
    updated = await UserRepo(conn).update_profile(user.id, **body.model_dump(exclude_unset=True))
    if updated is None:
        raise not_found("user")
    return await _profile_response(conn, updated)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    if user.auth_provider != "local":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "password managed by your identity provider"
        )
    verified = await LocalIdentityProvider(conn).authenticate(user.email, body.current_password)
    if verified is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "current password incorrect")
    repo = UserRepo(conn)
    await repo.set_password_hash(user.id, hash_password(body.new_password))
    await repo.set_must_change_password(user.id, False)
    await AuthSessionRepo(conn).revoke_all(user.id)
    await _audit(conn, "password.changed", str(user.id), actor=user.id, org_id=user.org_id)


@router.put("/locale", response_model=User)
async def set_my_locale(
    body: LocaleRequest,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> User:
    if body.locale not in SUPPORTED_LOCALES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unsupported locale")
    updated = await UserRepo(conn).set_locale(user.id, body.locale)
    if updated is None:
        raise not_found("user")
    return updated


@router.put("/me/preferences", response_model=User)
async def set_my_preferences(
    body: PreferencesUpdate,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> User:
    """Update the current user's chat answer style / preferred model. Only fields
    present in the body are written (preferred_chat_model may be set to null to
    clear a pin). answer_style is validated against the allowed set."""
    fields: dict[str, object] = {}
    sent = body.model_fields_set
    if "answer_style" in sent:
        if body.answer_style not in ANSWER_STYLES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"answer_style must be one of {', '.join(ANSWER_STYLES)}",
            )
        fields["answer_style"] = body.answer_style
    if "preferred_chat_model" in sent:
        model = body.preferred_chat_model
        if model is not None:
            model = model.strip() or None
        if model is not None:
            config = await SettingsRepo(conn).get_ai_config()
            if model not in config["chat_allowed_models"]:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "preferred_chat_model is not allowed",
                )
        fields["preferred_chat_model"] = model
    if not fields:
        return user
    updated = await UserRepo(conn).update_profile(user.id, **fields)
    if updated is None:
        raise not_found("user")
    return updated


class MemoryItem(BaseModel):
    content: str
    created_at: datetime


@router.get("/memory", response_model=list[MemoryItem])
async def list_my_memory(
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[MemoryItem]:
    rows = await UserMemoryRepo(conn).list(user.id)
    return [MemoryItem(content=r["content"], created_at=r["created_at"]) for r in rows]


@router.delete("/memory", status_code=status.HTTP_204_NO_CONTENT)
async def clear_my_memory(
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    await UserMemoryRepo(conn).clear(user.id)


class MemoryImportIn(BaseModel):
    text: str


class MemoryImportOut(BaseModel):
    added: int


@router.post("/memory/import", response_model=MemoryImportOut)
async def import_my_memory(
    body: MemoryImportIn,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> MemoryImportOut:
    """Import durable memory facts from a structured summary the user pasted from
    another AI assistant. The text is parsed (no LLM) into atomic facts and
    inserted with the same dedupe semantics the edge memory writer uses. Returns
    how many facts were actually added (duplicates of existing memory count 0).
    Refused while MEMORY_ENABLED is off; listing and clearing stay available so
    users can always see and erase what is stored about them."""
    if not settings.memory_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory is not enabled")
    text = body.text.strip()
    if not text:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "text is required")
    if len(text) > MEMORY_IMPORT_MAX_TEXT:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"text too long (max {MEMORY_IMPORT_MAX_TEXT} characters)",
        )
    facts = _parse_memory_import(text)
    if not facts:
        return MemoryImportOut(added=0)
    added = await UserMemoryRepo(conn).add(user.id, facts)
    return MemoryImportOut(added=added)


# ---- Invite management (admin) --------------------------------------------


@router.get("/invites", response_model=list[InviteListItem])
async def list_invites(
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[InviteListItem]:
    if user.role not in (Role.SUPER_ADMIN, Role.WORKSPACE_ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
    if user.org_id is None:
        return []
    invites = await InviteRepo(conn).list_by_org(user.org_id)
    return [
        InviteListItem(
            id=i.id,
            email=i.email,
            role=str(i.role),
            status=_invite_status(i),
            full_name=i.full_name,
            department=i.department,
            expires_at=i.expires_at,
            created_at=i.created_at,
        )
        for i in invites
    ]


@router.post("/invites/{invite_id}/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: UUID,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    if user.role not in (Role.SUPER_ADMIN, Role.WORKSPACE_ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
    invite = await InviteRepo(conn).get(invite_id)
    if invite is None or invite.org_id != user.org_id:
        raise not_found("invite")
    if not await InviteRepo(conn).revoke(invite_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "invite is no longer pending")


@router.post("/invites/{invite_id}/resend", response_model=InviteOut)
async def resend_invite(
    invite_id: UUID,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> InviteOut:
    if user.role not in (Role.SUPER_ADMIN, Role.WORKSPACE_ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
    old = await InviteRepo(conn).get(invite_id)
    if old is None or old.org_id != user.org_id:
        raise not_found("invite")
    # Re-issuing is granting anew: a WORKSPACE_ADMIN must not mint a fresh token
    # for a SUPER_ADMIN invite someone else created.
    await _check_invite_grant(
        conn, user, old.role, old.team_id, old.workspace_id, old.manager_user_id
    )
    await InviteRepo(conn).revoke(invite_id)  # best-effort: kill the old token
    profile = {k: getattr(old, k) for k in _INVITE_PROFILE_KEYS if getattr(old, k) is not None}
    invite, raw = await AuthService(conn).create_invite(
        old.org_id,
        old.email,
        str(old.role),
        created_by=user.id,
        team_id=old.team_id,
        workspace_id=old.workspace_id,
        **profile,
    )
    return InviteOut(
        invite_id=invite.id,
        token=raw,
        accept_path=f"/onboarding/accept?token={raw}",
        expires_at=invite.expires_at.isoformat(),
    )


@router.get("/invites/{token}/preview", response_model=InvitePreview)
async def preview_invite(token: str, conn: asyncpg.Connection = Depends(get_conn)) -> InvitePreview:
    """Public: the display profile seeded on a valid, unredeemed invite token so
    the accept page can pre-fill. Never reveals the token hash or internal ids."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    invite = await InviteRepo(conn).get_valid_by_hash(token_hash)
    if invite is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invalid or expired invite")
    return InvitePreview(
        email=invite.email,
        role=str(invite.role),
        full_name=invite.full_name,
        job_title=invite.job_title,
        department=invite.department,
        phone=invite.phone,
        location=invite.location,
        timezone=invite.timezone,
        locale=invite.locale,
    )


class AcceptIn(BaseModel):
    token: str
    password: NewPassword
    # Profile fields the invitee may fill for blanks the admin left empty.
    full_name: str | None = None
    job_title: str | None = None
    department: str | None = None
    phone: str | None = None
    location: str | None = None
    timezone: str | None = None
    locale: str | None = None


@router.post("/invites/accept", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
async def accept_invite(body: AcceptIn, conn: asyncpg.Connection = Depends(get_conn)) -> TokenOut:
    overrides = body.model_dump(
        include={
            "full_name",
            "job_title",
            "department",
            "phone",
            "location",
            "timezone",
            "locale",
        },
        exclude_none=True,
    )
    result = await AuthService(conn).accept_invite(body.token, body.password, overrides)
    if result is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired invite")
    _user, tokens = result
    await _audit(conn, "invite.accepted", _user.email, actor=_user.id, org_id=_user.org_id)
    return TokenOut(access_token=tokens.access_token, refresh_token=tokens.refresh_token)
