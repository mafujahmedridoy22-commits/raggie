# SPDX-License-Identifier: Apache-2.0

"""Users, external identities, auth sessions, invites and user memory."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg

from raggie_control_plane.config import settings
from raggie_control_plane.db.repositories._common import compose_sql, patch_set
from raggie_control_plane.models import Invite, User


class UserRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    # Rich profile columns (0003) + managed directory FKs (0009) an admin/invite
    # may seed at creation. department/location TEXT stay populated (write-through
    # at the API layer) since the edge reads them for the chat persona.
    _PROFILE_FIELDS = (
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
    )

    async def create(
        self,
        email: str,
        password_hash: str,
        role: str,
        *,
        org_id: UUID | None = None,
        must_change_password: bool = False,
        is_platform_admin: bool = False,
        **profile: object,
    ) -> User:
        # Only the supplied profile fields are written; the rest take DB defaults.
        # A user without an explicit locale gets the install default.
        profile.setdefault("locale", None)
        if profile["locale"] is None:
            profile["locale"] = settings.install_locale
        cols = [
            "email",
            "password_hash",
            "role",
            "org_id",
            "must_change_password",
            "is_platform_admin",
        ]
        vals: list[object] = [
            email,
            password_hash,
            role,
            org_id,
            must_change_password,
            is_platform_admin,
        ]
        for field in self._PROFILE_FIELDS:
            if profile.get(field) is not None:
                cols.append(field)
                vals.append(profile[field])
        row = await self.conn.fetchrow(
            compose_sql(
                "INSERT INTO users ({cols}) VALUES ({ph}) RETURNING *",
                cols=cols,
                ph=[f"${i}" for i in range(1, len(vals) + 1)],
            ),
            *vals,
        )
        return User(**dict(row))

    async def get(self, user_id: UUID) -> User | None:
        row = await self.conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return User(**dict(row)) if row else None

    async def get_by_email(self, email: str) -> User | None:
        row = await self.conn.fetchrow("SELECT * FROM users WHERE email = $1", email)
        return User(**dict(row)) if row else None

    async def get_password_hash(self, email: str) -> str | None:
        row = await self.conn.fetchrow("SELECT password_hash FROM users WHERE email = $1", email)
        return row["password_hash"] if row else None

    async def set_password_hash(self, user_id: UUID, password_hash: str) -> None:
        await self.conn.execute(
            "UPDATE users SET password_hash = $2 WHERE id = $1", user_id, password_hash
        )

    async def super_admin_exists(self) -> bool:
        row = await self.conn.fetchrow("SELECT 1 FROM users WHERE role = 'SUPER_ADMIN' LIMIT 1")
        return row is not None

    # ── SSO / SCIM provisioning ───────────────────────────────────────────────
    # Externally-managed users carry a sentinel password hash so password login
    # can never succeed for them.
    SSO_SENTINEL = "!"

    async def get_by_external_id(self, external_id: str) -> User | None:
        row = await self.conn.fetchrow("SELECT * FROM users WHERE external_id = $1", external_id)
        return User(**dict(row)) if row else None

    async def provision_external(
        self,
        email: str,
        role: str,
        *,
        org_id: UUID | None,
        auth_provider: str,
        external_id: str | None = None,
    ) -> User:
        """Idempotently provision an IdP/SCIM user (matched on email). Existing
        users are reactivated and re-stamped with the provider/external id."""
        row = await self.conn.fetchrow(
            "INSERT INTO users"
            " (email, password_hash, role, org_id, auth_provider, external_id, is_active, locale)"
            " VALUES ($1, $2, $3, $4, $5, $6, TRUE, $7)"
            " ON CONFLICT (email) DO UPDATE SET"
            "   auth_provider = EXCLUDED.auth_provider,"
            "   external_id = COALESCE(EXCLUDED.external_id, users.external_id),"
            "   is_active = TRUE, updated_at = now()"
            " RETURNING *",
            email,
            self.SSO_SENTINEL,
            role,
            org_id,
            auth_provider,
            external_id,
            settings.install_locale,
        )
        return User(**dict(row))

    async def create_external(
        self,
        email: str,
        role: str,
        *,
        org_id: UUID | None,
        auth_provider: str,
        external_id: str | None = None,
    ) -> User:
        """Create a new IdP-managed user (sentinel password). Unlike
        provision_external this never touches an existing account."""
        row = await self.conn.fetchrow(
            "INSERT INTO users"
            " (email, password_hash, role, org_id, auth_provider, external_id, locale)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
            email,
            self.SSO_SENTINEL,
            role,
            org_id,
            auth_provider,
            external_id,
            settings.install_locale,
        )
        return User(**dict(row))

    async def list(
        self, *, org_id: UUID | None = None, limit: int = 100, offset: int = 0
    ) -> list[User]:
        if org_id is not None:
            rows = await self.conn.fetch(
                "SELECT * FROM users WHERE org_id = $1 ORDER BY created_at LIMIT $2 OFFSET $3",
                org_id,
                limit,
                offset,
            )
        else:
            rows = await self.conn.fetch(
                "SELECT * FROM users ORDER BY created_at LIMIT $1 OFFSET $2", limit, offset
            )
        return [User(**dict(r)) for r in rows]

    async def set_active(self, user_id: UUID, active: bool) -> User | None:
        async with self.conn.transaction():
            row = await self.conn.fetchrow(
                "UPDATE users SET is_active = $2, updated_at = now() WHERE id = $1 RETURNING *",
                user_id,
                active,
            )
            if row is not None and not active:
                await AuthSessionRepo(self.conn).revoke_all(user_id)
        return User(**dict(row)) if row else None

    # Columns update_profile may patch. A dynamic SET (not COALESCE) lets an admin
    # clear a field by passing it explicitly as None.
    _UPDATABLE_FIELDS = (
        "email",
        "role",
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
        "answer_style",
        "preferred_chat_model",
    )

    async def update_profile(self, user_id: UUID, **fields: object) -> User | None:
        """Patch only the supplied columns (a key present is written verbatim,
        including None to clear it; absent keys are untouched)."""
        sets, vals = patch_set(self._UPDATABLE_FIELDS, fields, [user_id])
        if not sets:
            return await self.get(user_id)
        async with self.conn.transaction():
            row = await self.conn.fetchrow(
                compose_sql(
                    "UPDATE users SET {sets}, updated_at = now() WHERE id = $1 RETURNING *",
                    sets=sets,
                ),
                *vals,
            )
            if row is not None and "role" in fields:
                await AuthSessionRepo(self.conn).revoke_all(user_id)
        return User(**dict(row)) if row else None

    async def lock_active_platform_admins(self) -> list[UUID]:
        """Lock (FOR UPDATE) and return the active platform admins, so a
        concurrent revoke cannot race past the "never zero" invariant."""
        rows = await self.conn.fetch(
            "SELECT id FROM users WHERE is_platform_admin AND is_active ORDER BY id FOR UPDATE"
        )
        return [r["id"] for r in rows]

    async def set_platform_admin(self, user_id: UUID, value: bool) -> User | None:
        row = await self.conn.fetchrow(
            "UPDATE users SET is_platform_admin = $2, updated_at = now() WHERE id = $1 RETURNING *",
            user_id,
            value,
        )
        return User(**dict(row)) if row else None

    async def set_locale(self, user_id: UUID, locale: str) -> User | None:
        row = await self.conn.fetchrow(
            "UPDATE users SET locale = $2, updated_at = now() WHERE id = $1 RETURNING *",
            user_id,
            locale,
        )
        return User(**dict(row)) if row else None

    async def set_must_change_password(self, user_id: UUID, value: bool) -> None:
        await self.conn.execute(
            "UPDATE users SET must_change_password = $2, updated_at = now() WHERE id = $1",
            user_id,
            value,
        )

    async def delete(self, user_id: UUID) -> bool:
        result = await self.conn.execute("DELETE FROM users WHERE id = $1", user_id)
        return result.endswith("1")


class UserIdentityRepo:
    """External IdP identities (0024), keyed by the stable (issuer, subject)."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def get_user(self, issuer: str, subject: str) -> User | None:
        row = await self.conn.fetchrow(
            "SELECT u.* FROM user_identities i JOIN users u ON u.id = i.user_id"
            " WHERE i.issuer = $1 AND i.subject = $2",
            issuer,
            subject,
        )
        return User(**dict(row)) if row else None

    async def link(self, user_id: UUID, issuer: str, subject: str, email: str | None) -> None:
        await self.conn.execute(
            "INSERT INTO user_identities (user_id, issuer, subject, email) VALUES ($1, $2, $3, $4)",
            user_id,
            issuer,
            subject,
            email,
        )

    async def touch(self, issuer: str, subject: str, email: str | None) -> None:
        await self.conn.execute(
            "UPDATE user_identities SET last_login_at = now(), email = COALESCE($3, email)"
            " WHERE issuer = $1 AND subject = $2",
            issuer,
            subject,
            email,
        )


class AuthSessionRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def create(
        self,
        session_id: UUID,
        user_id: UUID,
        refresh_token_hash: str,
        expires_at: datetime,
    ) -> None:
        await self.conn.execute(
            "INSERT INTO auth_sessions"
            " (id, user_id, refresh_token_hash, expires_at)"
            " VALUES ($1, $2, $3, $4)",
            session_id,
            user_id,
            refresh_token_hash,
            expires_at,
        )

    async def is_active(self, session_id: UUID, user_id: UUID) -> bool:
        row = await self.conn.fetchrow(
            "SELECT 1 FROM auth_sessions"
            " WHERE id = $1 AND user_id = $2"
            "   AND revoked_at IS NULL AND expires_at > now()",
            session_id,
            user_id,
        )
        return row is not None

    async def rotate(
        self,
        session_id: UUID,
        user_id: UUID,
        expected_hash: str,
        new_hash: str,
        expires_at: datetime,
    ) -> bool:
        # The outgoing hash is kept as the predecessor (with rotated_at) so a
        # concurrent duplicate refresh can be told apart from token theft.
        row = await self.conn.fetchrow(
            "UPDATE auth_sessions"
            " SET previous_refresh_token_hash = refresh_token_hash, rotated_at = now(),"
            "     refresh_token_hash = $4, expires_at = $5, updated_at = now()"
            " WHERE id = $1 AND user_id = $2 AND refresh_token_hash = $3"
            "   AND revoked_at IS NULL AND expires_at > now()"
            " RETURNING id",
            session_id,
            user_id,
            expected_hash,
            new_hash,
            expires_at,
        )
        return row is not None

    async def rotate_from_predecessor(
        self,
        session_id: UUID,
        user_id: UUID,
        presented_hash: str,
        new_hash: str,
        expires_at: datetime,
        grace_seconds: int,
    ) -> bool:
        """Benign-race path: the presented token is the session's immediate
        predecessor, rotated out less than ``grace_seconds`` ago (two web
        replicas refreshing at once). Rotate again so the caller gets a working
        pair and the session family stays alive. True when it applied."""
        row = await self.conn.fetchrow(
            "UPDATE auth_sessions"
            " SET previous_refresh_token_hash = refresh_token_hash, rotated_at = now(),"
            "     refresh_token_hash = $4, expires_at = $5, updated_at = now()"
            " WHERE id = $1 AND user_id = $2 AND previous_refresh_token_hash = $3"
            "   AND rotated_at > now() - make_interval(secs => $6)"
            "   AND revoked_at IS NULL AND expires_at > now()"
            " RETURNING id",
            session_id,
            user_id,
            presented_hash,
            new_hash,
            expires_at,
            grace_seconds,
        )
        return row is not None

    async def revoke_on_reuse(self, session_id: UUID, user_id: UUID, presented_hash: str) -> bool:
        """Refresh-token reuse detection. A validly signed refresh token for this
        session that is no longer the current one was already rotated out, so a
        second party holds a copy: revoke the session (the whole token family).
        True when a live session was revoked."""
        row = await self.conn.fetchrow(
            "UPDATE auth_sessions SET revoked_at = now(), updated_at = now()"
            " WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL"
            "   AND refresh_token_hash <> $3 RETURNING id",
            session_id,
            user_id,
            presented_hash,
        )
        return row is not None

    async def revoke(self, session_id: UUID, user_id: UUID) -> bool:
        row = await self.conn.fetchrow(
            "UPDATE auth_sessions SET revoked_at = now(), updated_at = now()"
            " WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL RETURNING id",
            session_id,
            user_id,
        )
        return row is not None

    async def revoke_all(self, user_id: UUID) -> int:
        result = await self.conn.execute(
            "UPDATE auth_sessions SET revoked_at = now(), updated_at = now()"
            " WHERE user_id = $1 AND revoked_at IS NULL",
            user_id,
        )
        return int(result.rsplit(" ", 1)[-1])


class InviteRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    # Optional admin-seeded profile carried by the invite (0003).
    _PROFILE_FIELDS = (
        "full_name",
        "job_title",
        "department",
        "manager_user_id",
        "phone",
        "location",
        "timezone",
        "locale",
    )

    async def create(
        self,
        org_id: UUID,
        email: str,
        role: str,
        token_hash: str,
        *,
        expires_at: datetime,
        created_by: UUID | None = None,
        team_id: UUID | None = None,
        workspace_id: UUID | None = None,
        **profile: object,
    ) -> Invite:
        cols = [
            "org_id",
            "email",
            "role",
            "team_id",
            "workspace_id",
            "token_hash",
            "expires_at",
            "created_by",
        ]
        vals: list[object] = [
            org_id,
            email,
            role,
            team_id,
            workspace_id,
            token_hash,
            expires_at,
            created_by,
        ]
        for field in self._PROFILE_FIELDS:
            if profile.get(field) is not None:
                cols.append(field)
                vals.append(profile[field])
        row = await self.conn.fetchrow(
            compose_sql(
                "INSERT INTO invites ({cols}) VALUES ({ph}) RETURNING *",
                cols=cols,
                ph=[f"${i}" for i in range(1, len(vals) + 1)],
            ),
            *vals,
        )
        return Invite(**dict(row))

    async def get(self, invite_id: UUID) -> Invite | None:
        row = await self.conn.fetchrow("SELECT * FROM invites WHERE id = $1", invite_id)
        return Invite(**dict(row)) if row else None

    async def list_by_org(self, org_id: UUID) -> list[Invite]:
        rows = await self.conn.fetch(
            "SELECT * FROM invites WHERE org_id = $1 ORDER BY created_at DESC", org_id
        )
        return [Invite(**dict(r)) for r in rows]

    async def get_valid_by_hash(self, token_hash: str) -> Invite | None:
        row = await self.conn.fetchrow(
            "SELECT * FROM invites"
            " WHERE token_hash = $1 AND accepted_at IS NULL"
            "   AND revoked_at IS NULL AND expires_at > now()",
            token_hash,
        )
        return Invite(**dict(row)) if row else None

    async def revoke(self, invite_id: UUID) -> bool:
        """Revoke a still-pending invite. False if already accepted/revoked."""
        result = await self.conn.execute(
            "UPDATE invites SET revoked_at = now()"
            " WHERE id = $1 AND accepted_at IS NULL AND revoked_at IS NULL",
            invite_id,
        )
        return result.endswith("1")

    async def mark_accepted(self, invite_id: UUID) -> None:
        await self.conn.execute("UPDATE invites SET accepted_at = now() WHERE id = $1", invite_id)


class UserMemoryRepo:
    """Durable per-user memory facts (auto-extracted in the edge; viewed/cleared
    here by the user from Account settings)."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def list(self, user_id: UUID, *, limit: int = 100) -> list[dict]:
        rows = await self.conn.fetch(
            "SELECT content, created_at FROM user_memory WHERE user_id = $1"
            " ORDER BY created_at DESC LIMIT $2",
            user_id,
            limit,
        )
        return [{"content": r["content"], "created_at": r["created_at"]} for r in rows]

    async def clear(self, user_id: UUID) -> int:
        result = await self.conn.execute("DELETE FROM user_memory WHERE user_id = $1", user_id)
        try:
            return int(result.split()[-1])  # asyncpg returns e.g. "DELETE 3"
        except (ValueError, IndexError):
            return 0

    async def add(self, user_id: UUID, facts: list[str]) -> int:
        """Insert deduped memory facts for a user (matching the edge writer's
        ``ON CONFLICT (user_id, content) DO NOTHING`` semantics). Returns the
        number of rows actually inserted (existing/duplicate facts count 0)."""
        added = 0
        for fact in facts:
            result = await self.conn.execute(
                "INSERT INTO user_memory (user_id, content) VALUES ($1, $2)"
                " ON CONFLICT (user_id, content) DO NOTHING",
                user_id,
                fact,
            )
            if result.endswith("1"):  # asyncpg returns "INSERT 0 1" on a real insert
                added += 1
        return added
