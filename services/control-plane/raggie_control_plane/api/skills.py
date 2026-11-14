# SPDX-License-Identifier: Apache-2.0

"""Versioned assistant execution policies invoked via a slash keyword.

The browser sends only ``skill_id`` + ``version``. The edge resolves the immutable
snapshot, tenant/owner visibility, model policy, and tool allow-list server-side.

Org-scoped. Any logged-in user reads the enabled skills of their own org (the chat
picker); a WORKSPACE_ADMIN of that org (or a SUPER_ADMIN) manages the full catalog.
Slugs are a short lowercase token, unique per org → a duplicate slug returns 409.
"""

from __future__ import annotations

import re
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from raggie_control_plane.api.deps import get_conn, get_current_user
from raggie_control_plane.api.errors import not_found
from raggie_control_plane.authz import Action, Authorizer
from raggie_control_plane.db.repositories import SettingsRepo, SkillsRepo
from raggie_control_plane.models import Skill, SkillVersion, User

router = APIRouter(prefix="/api/v1", tags=["skills"])

# A slug is a short lowercase token usable as a typed slash-command keyword.
_SLUG_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_TOOL_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,128}$")
_ALLOWED_STYLES = {"corporate", "short", "instructive", "balanced"}


class SkillIn(BaseModel):
    name: str = Field(max_length=120)
    slug: str
    description: str | None = Field(default=None, max_length=2000)
    instructions: str = Field(max_length=16000)
    icon: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=200)
    answer_style: str | None = None
    allowed_tools: list[str] = Field(default_factory=list, max_length=64)
    enabled: bool = True


class SkillPatch(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    slug: str | None = None
    description: str | None = Field(default=None, max_length=2000)
    instructions: str | None = Field(default=None, max_length=16000)
    icon: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=200)
    answer_style: str | None = None
    allowed_tools: list[str] | None = Field(default=None, max_length=64)
    enabled: bool | None = None


def _clean_required(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{field} required")
    return cleaned


def _clean_slug(slug: str) -> str:
    cleaned = slug.strip().lower()
    if not _SLUG_RE.match(cleaned):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "slug must be a short lowercase token (a-z, 0-9, -; up to 32 chars)",
        )
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


def _validate_allowed_tools(tools: list[str] | None) -> list[str]:
    """Canonicalize a default-deny tool allow-list for stable policy hashes."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tools or []:
        name = raw.strip()
        if not _TOOL_RE.fullmatch(name):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "allowed_tools entries must be 1-128 character tool identifiers",
            )
        if name not in seen:
            seen.add(name)
            out.append(name)
    return sorted(out)


async def _validate_model(model: str | None, conn: asyncpg.Connection) -> str | None:
    """A pinned model must belong to the platform-curated provider allow-list."""
    if model is None:
        return None
    cleaned = model.strip()
    if not cleaned:
        return None
    cfg = await SettingsRepo(conn).get_ai_config()
    allowed = cfg["chat_allowed_models"]
    if allowed and cleaned not in allowed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "model is not in the platform allowed-model list",
        )
    return cleaned


def _validate_required_patch(fields: dict[str, object], field: str) -> None:
    if field in fields:
        value = fields[field]
        if not isinstance(value, str):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{field} required")
        fields[field] = _clean_required(value, field)


async def _require_skills_admin(
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> User:
    """Guard for the builder endpoints: WORKSPACE_ADMIN of the caller's own org
    (or a SUPER_ADMIN). Skills are org-scoped, so the caller must belong to an
    org — a platform super-admin with no org has no catalog to manage (400)."""
    if user.org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no organization in scope")
    if not await Authorizer(conn).resolve(user, Action.MANAGE_ORG, org_id=user.org_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    return user


# ── Chat picker: enabled skills for the caller's org ─────────────
@router.get("/skills", response_model=list[Skill])
async def list_skills(
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[Skill]:
    """Any logged-in user: the UNION of the enabled org-published skills and the
    caller's OWN enabled personal skills (the chat skill picker). A user with no
    org (a platform super-admin) simply gets [].
    """
    if user.org_id is None:
        return []
    return await SkillsRepo(conn).list_effective(user.org_id, user.id)


# ── Personal skills: any user manages their OWN skills ───────────
@router.get("/skills/personal", response_model=list[Skill])
async def list_personal_skills(
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[Skill]:
    """The caller's own personal skills (any enabled state) — their mgmt UI."""
    if user.org_id is None:
        return []
    return await SkillsRepo(conn).list_personal(user.org_id, user.id)


@router.post(
    "/skills/personal",
    response_model=Skill,
    status_code=status.HTTP_201_CREATED,
)
async def create_personal_skill(
    body: SkillIn,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> Skill:
    """Create a personal skill owned by the caller (NOT admin-gated)."""
    if user.org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no organization in scope")
    name = _clean_required(body.name, "name")
    slug = _clean_slug(body.slug)
    instructions = _clean_required(body.instructions, "instructions")
    answer_style = _validate_answer_style(body.answer_style)
    model = await _validate_model(body.model, conn)
    allowed_tools = _validate_allowed_tools(body.allowed_tools)
    try:
        return await SkillsRepo(conn).create_personal(
            user.org_id,
            user.id,
            name=name,
            slug=slug,
            instructions=instructions,
            description=body.description,
            icon=body.icon,
            model=model,
            answer_style=answer_style,
            allowed_tools=allowed_tools,
            enabled=body.enabled,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "slug already in use") from exc


@router.put("/skills/personal/{skill_id}", response_model=Skill)
async def update_personal_skill(
    skill_id: UUID,
    body: SkillPatch,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> Skill:
    if user.org_id is None:
        raise not_found("skill")
    fields = body.model_dump(exclude_unset=True)
    _validate_required_patch(fields, "name")
    if "slug" in fields and fields["slug"] is not None:
        fields["slug"] = _clean_slug(fields["slug"])
    elif "slug" in fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
    _validate_required_patch(fields, "instructions")
    if "answer_style" in fields:
        fields["answer_style"] = _validate_answer_style(fields["answer_style"])
    if "model" in fields:
        fields["model"] = await _validate_model(fields["model"], conn)
    if "allowed_tools" in fields:
        fields["allowed_tools"] = _validate_allowed_tools(fields["allowed_tools"])
    try:
        updated = await SkillsRepo(conn).update_personal(skill_id, user.id, **fields)
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "slug already in use") from exc
    # Missing or not owned by the caller → invisible (404).
    if updated is None:
        raise not_found("skill")
    return updated


@router.delete(
    "/skills/personal/{skill_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_personal_skill(
    skill_id: UUID,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    if user.org_id is None or not await SkillsRepo(conn).delete_personal(skill_id, user.id):
        raise not_found("skill")


@router.get(
    "/skills/personal/{skill_id}/versions",
    response_model=list[SkillVersion],
)
async def list_personal_skill_versions(
    skill_id: UUID,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[SkillVersion]:
    repo = SkillsRepo(conn)
    skill = await repo.get(skill_id)
    if skill is None or skill.owner_user_id != user.id:
        raise not_found("skill")
    return await repo.list_versions(skill_id)


@router.post(
    "/skills/personal/{skill_id}/versions/{version}/restore",
    response_model=Skill,
)
async def restore_personal_skill_version(
    skill_id: UUID,
    version: int,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> Skill:
    repo = SkillsRepo(conn)
    skill = await repo.get(skill_id)
    if skill is None or skill.owner_user_id != user.id:
        raise not_found("skill")
    restored = await repo.restore_version(skill_id, version, actor_id=user.id)
    if restored is None:
        raise not_found("skill version")
    return restored


# ── Builder: full catalog + CRUD (WORKSPACE_ADMIN+) ──────────────
@router.get("/admin/skills", response_model=list[Skill])
async def list_admin_skills(
    user: User = Depends(_require_skills_admin),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[Skill]:
    """All skills (including disabled) for the caller's org — the builder UI."""
    return await SkillsRepo(conn).list_for_org(user.org_id)


@router.post(
    "/admin/skills",
    response_model=Skill,
    status_code=status.HTTP_201_CREATED,
)
async def create_skill(
    body: SkillIn,
    user: User = Depends(_require_skills_admin),
    conn: asyncpg.Connection = Depends(get_conn),
) -> Skill:
    name = _clean_required(body.name, "name")
    slug = _clean_slug(body.slug)
    instructions = _clean_required(body.instructions, "instructions")
    answer_style = _validate_answer_style(body.answer_style)
    model = await _validate_model(body.model, conn)
    allowed_tools = _validate_allowed_tools(body.allowed_tools)
    try:
        return await SkillsRepo(conn).create(
            user.org_id,
            name=name,
            slug=slug,
            instructions=instructions,
            description=body.description,
            icon=body.icon,
            model=model,
            answer_style=answer_style,
            allowed_tools=allowed_tools,
            enabled=body.enabled,
            created_by=user.id,
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "slug already in use") from exc


@router.put("/admin/skills/{skill_id}", response_model=Skill)
async def update_skill(
    skill_id: UUID,
    body: SkillPatch,
    user: User = Depends(_require_skills_admin),
    conn: asyncpg.Connection = Depends(get_conn),
) -> Skill:
    repo = SkillsRepo(conn)
    skill = await repo.get(skill_id)
    # A skill from another org is invisible (404), not merely forbidden.
    if skill is None or skill.org_id != user.org_id:
        raise not_found("skill")
    fields = body.model_dump(exclude_unset=True)
    _validate_required_patch(fields, "name")
    if "slug" in fields and fields["slug"] is not None:
        fields["slug"] = _clean_slug(fields["slug"])
    elif "slug" in fields:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slug required")
    _validate_required_patch(fields, "instructions")
    if "answer_style" in fields:
        fields["answer_style"] = _validate_answer_style(fields["answer_style"])
    if "model" in fields:
        fields["model"] = await _validate_model(fields["model"], conn)
    if "allowed_tools" in fields:
        fields["allowed_tools"] = _validate_allowed_tools(fields["allowed_tools"])
    try:
        updated = await repo.update(skill_id, actor_id=user.id, **fields)
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "slug already in use") from exc
    if updated is None:
        raise not_found("skill")
    return updated


@router.get(
    "/admin/skills/{skill_id}/versions",
    response_model=list[SkillVersion],
)
async def list_skill_versions(
    skill_id: UUID,
    user: User = Depends(_require_skills_admin),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[SkillVersion]:
    repo = SkillsRepo(conn)
    skill = await repo.get(skill_id)
    if skill is None or skill.org_id != user.org_id:
        raise not_found("skill")
    return await repo.list_versions(skill_id)


@router.post(
    "/admin/skills/{skill_id}/versions/{version}/restore",
    response_model=Skill,
)
async def restore_skill_version(
    skill_id: UUID,
    version: int,
    user: User = Depends(_require_skills_admin),
    conn: asyncpg.Connection = Depends(get_conn),
) -> Skill:
    repo = SkillsRepo(conn)
    skill = await repo.get(skill_id)
    if skill is None or skill.org_id != user.org_id:
        raise not_found("skill")
    restored = await repo.restore_version(skill_id, version, actor_id=user.id)
    if restored is None:
        raise not_found("skill version")
    return restored


@router.delete(
    "/admin/skills/{skill_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_skill(
    skill_id: UUID,
    user: User = Depends(_require_skills_admin),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    repo = SkillsRepo(conn)
    skill = await repo.get(skill_id)
    if skill is None or skill.org_id != user.org_id:
        raise not_found("skill")
    await repo.delete(skill_id, actor_id=user.id)
