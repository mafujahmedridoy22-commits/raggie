# SPDX-License-Identifier: Apache-2.0

"""Current-user workspace listing + per-workspace widget / CORS configuration."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit
from uuid import UUID

import asyncpg
import idna
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from raggie_control_plane.api.deps import get_conn, get_current_user, require_workspace
from raggie_control_plane.api.errors import not_found
from raggie_control_plane.authz import Action, visible_workspaces
from raggie_control_plane.config import settings
from raggie_control_plane.db.repositories import WorkspaceRepo
from raggie_control_plane.models import User, Workspace, WorkspaceType

router = APIRouter(prefix="/api/v1", tags=["workspaces"])

# A widget is locked to explicit origins (scheme://host[:port]); "*" would let any
# site embed it. The edge compares the browser's Origin header byte-for-byte,
# so origins are stored exactly as browsers serialize them.
_DEFAULT_PORTS = {"http": 80, "https": 443}
_ASCII_HOST_RE = re.compile(r"^[a-z0-9_\-]+(\.[a-z0-9_\-]+)*\.?$")
_NUMERIC_LABEL_RE = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")


@router.get("/workspaces", response_model=list[Workspace])
async def my_workspaces(
    user: User = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[Workspace]:
    return await visible_workspaces(conn, user)


# ── Widget / CORS configuration (admin surface) ──────────────────────────────


def _normalize_host(host: str) -> str:
    if host.startswith("["):  # IPv6 literal, kept bracketed like browsers do
        try:
            return f"[{ipaddress.IPv6Address(host[1:-1]).compressed}]"
        except ValueError:
            raise ValueError("invalid IPv6 address") from None
    if not host.isascii():
        try:  # IDN -> punycode with the UTS #46 mapping browsers use
            host = idna.encode(host, uts46=True, transitional=False).decode("ascii")
        except idna.IDNAError:
            raise ValueError("invalid internationalized host") from None
    host = host.lower()
    if not _ASCII_HOST_RE.match(host):
        raise ValueError("invalid host")
    # A numeric last label makes the host IPv4 to a browser, which serializes it
    # dotted-decimal (0x7f.1 -> 127.0.0.1); only that canonical spelling matches.
    if _NUMERIC_LABEL_RE.match(host.rstrip(".").rsplit(".", 1)[-1]):
        try:
            canonical = str(ipaddress.IPv4Address(host))
        except ValueError:
            raise ValueError("invalid IPv4 address") from None
        if canonical != host:
            raise ValueError("non-canonical IPv4 address")
    return host


def normalize_origin(raw: str) -> str:
    """Serialize ``raw`` the way a browser serializes the Origin header:
    lowercase scheme and host, punycode for IDN hosts, no default port and no
    trailing slash. Raises ValueError for anything that is not a bare http(s)
    origin (path, query, fragment, userinfo, wildcard)."""
    value = raw.strip()
    if "*" in value:
        raise ValueError("wildcard origins are not allowed")
    if any(ch.isspace() or ord(ch) < 0x20 or ch in "\\?#@" for ch in value):
        raise ValueError("origins carry no userinfo, query or fragment")
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not value[len(scheme) :].startswith("://"):
        raise ValueError("expected http(s)://host[:port]")
    if parts.path not in ("", "/"):
        raise ValueError("origins have no path")
    # No userinfo (rejected above), so netloc is host[:port] or [v6][:port].
    if parts.netloc.startswith("["):
        host, _, rest = parts.netloc.partition("]")
        host += "]"
        if rest and not rest.startswith(":"):
            raise ValueError("invalid host")
        port_text = rest[1:] if rest else None
    else:
        host, sep, port_text = parts.netloc.partition(":")
        port_text = port_text if sep else None
    if not host:
        raise ValueError("missing host")
    origin = f"{scheme}://{_normalize_host(host)}"
    if port_text is not None:
        if not port_text.isdigit() or not 0 < int(port_text) <= 65535:
            raise ValueError("invalid port")
        if int(port_text) != _DEFAULT_PORTS[scheme]:
            origin += f":{int(port_text)}"
    return origin


def _validate_origins(origins: list[str]) -> list[str]:
    """Normalize every origin (dropping blanks and duplicates). Raises 422 on any
    invalid entry so a misconfiguration can never silently widen the boundary."""
    cleaned: list[str] = []
    for raw in origins:
        if not raw.strip():
            continue
        try:
            origin = normalize_origin(raw)
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"invalid origin, expected scheme://host[:port] ({exc}): {raw!r}",
            ) from None
        if origin not in cleaned:
            cleaned.append(origin)
    return cleaned


class WidgetConfigIn(BaseModel):
    widget_primary_color: str | None = None
    cors_allowed_origins: list[str] | None = None


class WidgetConfigOut(BaseModel):
    workspace_id: UUID
    type: WorkspaceType
    widget_primary_color: str
    cors_allowed_origins: list[str]


def _to_out(ws: Workspace) -> WidgetConfigOut:
    return WidgetConfigOut(
        workspace_id=ws.id,
        type=ws.type,
        widget_primary_color=ws.widget_primary_color,
        cors_allowed_origins=ws.cors_allowed_origins,
    )


@router.get(
    "/admin/workspaces/{workspace_id}/widget",
    response_model=WidgetConfigOut,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def get_widget_config(
    workspace_id: UUID,
    conn: asyncpg.Connection = Depends(get_conn),
) -> WidgetConfigOut:
    ws = await WorkspaceRepo(conn).get(workspace_id)
    if ws is None:
        raise not_found("workspace")
    return _to_out(ws)


@router.put(
    "/admin/workspaces/{workspace_id}/widget",
    response_model=WidgetConfigOut,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def put_widget_config(
    workspace_id: UUID,
    body: WidgetConfigIn,
    conn: asyncpg.Connection = Depends(get_conn),
) -> WidgetConfigOut:
    origins = (
        _validate_origins(body.cors_allowed_origins)
        if body.cors_allowed_origins is not None
        else None
    )
    ws = await WorkspaceRepo(conn).update_widget_config(
        workspace_id,
        widget_primary_color=body.widget_primary_color,
        cors_allowed_origins=origins,
    )
    if ws is None:
        raise not_found("workspace")
    return _to_out(ws)


# ── Chat-intelligence flags (web search / personalization / memory) ──────────
# Each is effective only when its global kill switch (edge env) is on too.


class WorkspaceSettingsIn(BaseModel):
    web_search_enabled: bool | None = None
    personalization_enabled: bool | None = None
    memory_enabled: bool | None = None


class WorkspaceSettingsOut(BaseModel):
    workspace_id: UUID
    web_search_enabled: bool
    personalization_enabled: bool
    memory_enabled: bool
    # MEMORY_ENABLED: false = memory is off platform-wide whatever the flag says.
    memory_available: bool


def _settings_out(ws: Workspace) -> WorkspaceSettingsOut:
    return WorkspaceSettingsOut(
        workspace_id=ws.id,
        web_search_enabled=ws.web_search_enabled,
        personalization_enabled=ws.personalization_enabled,
        memory_enabled=ws.memory_enabled,
        memory_available=settings.memory_enabled,
    )


@router.get(
    "/admin/workspaces/{workspace_id}/settings",
    response_model=WorkspaceSettingsOut,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def get_workspace_settings(
    workspace_id: UUID,
    conn: asyncpg.Connection = Depends(get_conn),
) -> WorkspaceSettingsOut:
    ws = await WorkspaceRepo(conn).get(workspace_id)
    if ws is None:
        raise not_found("workspace")
    return _settings_out(ws)


@router.put(
    "/admin/workspaces/{workspace_id}/settings",
    response_model=WorkspaceSettingsOut,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def put_workspace_settings(
    workspace_id: UUID,
    body: WorkspaceSettingsIn,
    conn: asyncpg.Connection = Depends(get_conn),
) -> WorkspaceSettingsOut:
    ws = await WorkspaceRepo(conn).update_settings(
        workspace_id,
        web_search_enabled=body.web_search_enabled,
        personalization_enabled=body.personalization_enabled,
        memory_enabled=body.memory_enabled,
    )
    if ws is None:
        raise not_found("workspace")
    return _settings_out(ws)
