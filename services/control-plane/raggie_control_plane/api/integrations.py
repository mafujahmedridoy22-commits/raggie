# SPDX-License-Identifier: Apache-2.0

"""Generic external-integration / MCP management (provider-agnostic).

The provider registry below is the single extension point: adding a connector is
just another entry (slack, notion, an MCP server, …) — the data model, CRUD, and
UI are all generic. Credentials are encrypted at rest (infra.crypto); this layer
never persists plaintext. Live data-sync per provider is layered on top later
(events → ingestion), reusing target_workspace_id scoping.
"""

from __future__ import annotations

import json
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from raggie_control_plane.api.deps import ensure_platform_admin, get_conn, get_current_user
from raggie_control_plane.api.errors import not_found
from raggie_control_plane.authz import is_platform_admin
from raggie_control_plane.db.repositories import (
    IntegrationRepo,
    MembershipRepo,
    TeamRepo,
    UserRepo,
    WorkspaceRepo,
)
from raggie_control_plane.infra import crypto
from raggie_control_plane.models import Integration, User

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])

# Every valid scope_type. 'org' (org-wide MCP/tool capability) is server-forced
# for org providers, never client-selected; the workspace-style scopes below are
# the ones a client may explicitly pick for a data-source provider.
_SCOPES = {"user", "team", "workspace", "org"}
_WORKSPACE_SCOPES = {"user", "team", "workspace"}


class ProviderField(BaseModel):
    key: str
    label: str
    secret: bool = False


class Provider(BaseModel):
    key: str
    label: str
    auth: str  # token | oauth | mcp
    description: str = ""
    category: str = "other"  # messaging | docs | storage | mcp | other
    icon: str = "extension"  # Material Symbols name for the marketplace card
    featured: bool = False
    # Connection scope: "org" (org-wide MCP/tool capability — connected once,
    # platform-admin-only, no workspace) vs "workspace" (a data source bound to a
    # user/team/workspace). The frontend reads this to decide whether to ask for
    # a workspace when connecting.
    scope: str = "workspace"  # org | workspace
    fields: list[ProviderField] = []


# The extension point. A deliberately small, MCP-first set: connect a tool by
# pasting its key/URL — not an app store. Secret fields are encrypted, the rest
# go in `config`. `featured` is kept on the model for compatibility but the UI no
# longer uses it.
PROVIDERS: list[Provider] = [
    # ── A couple of high-value key-based sources ─────────────────────────────
    # NOTE (Jira): live Jira API calls are made by a self-hosted
    # `mcp-atlassian` container configured via deploy-time env creds
    # (JIRA_URL / JIRA_USERNAME / JIRA_API_TOKEN), single-tenant — so the creds
    # stored on this integration row are NOT yet used for live calls; they
    # future-proof a later OAuth/multi-tenant phase. category="mcp" so it
    # surfaces in the MCP/featured marketplace section. The edge gates Jira tools
    # on an integrations row with provider='jira' AND status='connected'.
    Provider(
        key="jira",
        label="Jira",
        auth="token",
        category="mcp",
        icon="confirmation_number",
        featured=True,
        scope="org",
        description="Connect Jira Cloud so the assistant can search and read "
        "issues (updating them comes in a later step).",
        fields=[
            ProviderField(
                key="site_url", label="Jira site URL (https://your-company.atlassian.net)"
            ),
            ProviderField(key="email", label="Atlassian email"),
            ProviderField(key="api_token", label="API token", secret=True),
        ],
    ),
    Provider(
        key="slack",
        label="Slack",
        auth="token",
        category="messaging",
        icon="forum",
        description="Paste a bot token to make channels & messages searchable.",
        fields=[ProviderField(key="bot_token", label="Bot token", secret=True)],
    ),
    Provider(
        key="gdrive",
        label="Google Drive",
        auth="oauth",
        category="storage",
        icon="cloud",
        description="Connect Drive to make its documents searchable.",
        fields=[],
    ),
]
_PROVIDERS_BY_KEY = {p.key: p for p in PROVIDERS}


def _provider_or_404(key: str) -> Provider:
    p = _PROVIDERS_BY_KEY.get(key)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown provider")
    return p


async def _require_visible(
    conn: asyncpg.Connection, user: User, kind: str, entity_id: UUID
) -> None:
    """A referenced team / workspace / user must exist in the caller's org (a
    platform admin is global). Another org's entity is indistinguishable from a
    missing one (404)."""
    repo = {"team": TeamRepo, "workspace": WorkspaceRepo, "user": UserRepo}[kind](conn)
    entity = await repo.get(entity_id)
    if entity is None or (entity.org_id != user.org_id and not is_platform_admin(user)):
        raise not_found(kind)


async def _authorize(
    conn: asyncpg.Connection,
    user: User,
    scope_type: str,
    scope_id: UUID,
    target_workspace_id: UUID | None = None,
) -> None:
    """Org-scoped integrations (org-wide MCP/tool capabilities): platform admin
    only — they front the operator's deploy-time credential (e.g. the shared
    mcp-atlassian container), so flipping one to "connected", even with dummy
    credentials, would hand that credential to the org.

    Every other scope and the sync target must be in the caller's org (else
    404). Within it, a SUPER_ADMIN (or a platform admin) may manage any of
    them; anyone else only their own user scope, and a WORKSPACE_ADMIN also
    the teams and workspaces they effectively belong to. A sync target always
    needs effective access to that workspace."""
    if scope_type == "org":
        ensure_platform_admin(user)
        return
    await _require_visible(conn, user, scope_type, scope_id)
    if target_workspace_id is not None:
        await _require_visible(conn, user, "workspace", target_workspace_id)
    if user.role == "SUPER_ADMIN" or is_platform_admin(user):
        return

    members = MembershipRepo(conn)
    if scope_type == "user":
        if scope_id != user.id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    elif user.role != "WORKSPACE_ADMIN":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    elif scope_type == "team" and scope_id not in await members.effective_team_ids(user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if scope_type == "workspace" or target_workspace_id is not None:
        eff = set(await members.effective_workspace_ids(user.id))
        if scope_type == "workspace" and scope_id not in eff:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        if target_workspace_id is not None and target_workspace_id not in eff:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")


async def _scope_org(conn: asyncpg.Connection, scope_type: str, scope_id: UUID) -> UUID:
    """The org that owns the scope entity. An integration (and everything it
    syncs) belongs to that org, even when a platform admin connects it."""
    if scope_type == "workspace":
        ws = await WorkspaceRepo(conn).get(scope_id)
        org_id = ws.org_id if ws else None
    elif scope_type == "team":
        team = await TeamRepo(conn).get(scope_id)
        org_id = team.org_id if team else None
    else:
        owner = await UserRepo(conn).get(scope_id)
        org_id = owner.org_id if owner else None
    if org_id is None:
        raise not_found("scope")
    return org_id


class ConnectIn(BaseModel):
    provider: str
    # Omitted for org-scoped providers (server forces org scope). Required for
    # workspace-scoped (data-source) providers.
    scope_type: str | None = None
    scope_id: UUID | None = None
    target_workspace_id: UUID | None = None
    display_name: str | None = None
    credentials: dict[str, str] = {}  # secret fields → encrypted at rest
    config: dict = {}  # non-secret fields


class UpdateIn(BaseModel):
    display_name: str | None = None
    config: dict | None = None


@router.get("/providers", response_model=list[Provider])
async def list_providers(_: User = Depends(get_current_user)) -> list[Provider]:
    return PROVIDERS


class AvailableIntegration(BaseModel):
    """A connected, org-scoped MCP/tool integration as exposed to any org user
    for the chat "Tools" surface. Deliberately minimal: NO secrets, NO config,
    NO credentials — just enough to render an icon + name + a connected dot."""

    provider: str
    display_name: str
    icon: str
    status: str


@router.get("/available", response_model=list[AvailableIntegration])
async def list_available_integrations(
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[AvailableIntegration]:
    """Connected MCP/tool integrations for the caller's org, readable by ANY
    authenticated org user (not admin-only) — STAFF must see which org tools they
    can enable in chat. Returns only connected (status=='connected') integrations
    whose provider category is "mcp", and exposes ONLY {provider, display_name,
    icon, status} — never secrets/credentials_enc/config."""
    if user.org_id is None:
        return []
    integrations = await IntegrationRepo(conn).list_by_org(user.org_id)
    available: list[AvailableIntegration] = []
    for ig in integrations:
        if ig.status != "connected":
            continue
        provider = _PROVIDERS_BY_KEY.get(ig.provider)
        if provider is None or provider.category != "mcp":
            continue
        available.append(
            AvailableIntegration(
                provider=ig.provider,
                display_name=ig.display_name or provider.label,
                icon=provider.icon,
                status=ig.status,
            )
        )
    return available


@router.get("", response_model=list[Integration])
async def list_integrations(
    scope_type: str | None = Query(default=None),
    scope_id: UUID | None = Query(default=None),
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[Integration]:
    repo = IntegrationRepo(conn)
    if scope_type and scope_id:
        if scope_type not in _SCOPES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid scope_type")
        await _authorize(conn, user, scope_type, scope_id)
        return await repo.list_by_scope(scope_type, scope_id)
    # No scope filter: SUPER_ADMIN sees the whole org; others get nothing here
    # (they query their own scope explicitly).
    if user.role == "SUPER_ADMIN" and user.org_id is not None:
        return await repo.list_by_org(user.org_id)
    if scope_id is None and user.role != "SUPER_ADMIN":
        return await repo.list_by_scope("user", user.id)
    return []


@router.post("", response_model=Integration, status_code=status.HTTP_201_CREATED)
async def connect_integration(
    body: ConnectIn,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> Integration:
    provider = _provider_or_404(body.provider)
    if user.org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "user has no organization")

    # Resolve the effective scope of this connection. Org providers (MCP/tools)
    # are org-wide capabilities: platform-admin-only, connected once, with no
    # workspace — so the server forces the scope and ignores any client-sent
    # scope_type/scope_id/target_workspace_id. Workspace (data-source) providers
    # require an explicit user/team/workspace scope, authorized as before.
    if provider.scope == "org":
        ensure_platform_admin(user)
        scope_type = "org"
        scope_id = user.org_id
        target_workspace_id: UUID | None = None
        owner_org = user.org_id
    else:
        if body.scope_type not in _WORKSPACE_SCOPES or body.scope_id is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid scope_type")
        scope_type = body.scope_type
        scope_id = body.scope_id
        target_workspace_id = body.target_workspace_id
        await _authorize(conn, user, scope_type, scope_id, target_workspace_id)
        owner_org = await _scope_org(conn, scope_type, scope_id)
        if target_workspace_id is not None:
            target = await WorkspaceRepo(conn).get(target_workspace_id)
            if target is None or target.org_id != owner_org:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, "target workspace not in the scope's org"
                )

    credentials_enc: str | None = None
    if body.credentials:
        if not crypto.is_configured():
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "credential encryption is not configured (set INTEGRATION_ENC_KEY)",
            )
        credentials_enc = crypto.encrypt(json.dumps(body.credentials))

    # token/mcp providers are connected immediately; oauth would redirect (later).
    connected = credentials_enc is not None or provider.auth == "oauth"
    integration = await IntegrationRepo(conn).upsert(
        org_id=owner_org,
        scope_type=scope_type,
        scope_id=scope_id,
        provider=body.provider,
        target_workspace_id=target_workspace_id,
        display_name=body.display_name or provider.label,
        credentials_enc=credentials_enc,
        config=body.config,
        status="connected" if connected else "disconnected",
        created_by=user.id,
    )
    return integration


@router.patch("/{integration_id}", response_model=Integration)
async def update_integration(
    integration_id: UUID,
    body: UpdateIn,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> Integration:
    repo = IntegrationRepo(conn)
    existing = await repo.get(integration_id)
    if existing is None or existing.org_id != user.org_id:
        raise not_found("integration")
    await _authorize(
        conn, user, existing.scope_type, existing.scope_id, existing.target_workspace_id
    )
    updated = await repo.upsert(
        org_id=existing.org_id,
        scope_type=existing.scope_type,
        scope_id=existing.scope_id,
        provider=existing.provider,
        target_workspace_id=existing.target_workspace_id,
        display_name=body.display_name if body.display_name is not None else existing.display_name,
        credentials_enc=None,  # keep existing credentials
        config=body.config if body.config is not None else existing.config,
        status=existing.status,
        created_by=existing.created_by,
    )
    return updated


@router.delete("/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_integration(
    integration_id: UUID,
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> None:
    repo = IntegrationRepo(conn)
    existing = await repo.get(integration_id)
    if existing is None or existing.org_id != user.org_id:
        raise not_found("integration")
    await _authorize(
        conn, user, existing.scope_type, existing.scope_id, existing.target_workspace_id
    )
    await repo.delete(integration_id)
