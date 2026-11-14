# SPDX-License-Identifier: Apache-2.0

"""Documents and document folders."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from raggie_control_plane.db.repositories._common import compose_sql, patch_set
from raggie_control_plane.models import Document, DocumentFolder


class DocumentRepo:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def create(
        self,
        workspace_id: UUID,
        filename: str,
        file_size: int,
        qdrant_collection_name: str,
        *,
        object_key: str | None = None,
        folder_id: UUID | None = None,
        embedding_fingerprint: str | None = None,
    ) -> Document:
        row = await self.conn.fetchrow(
            "INSERT INTO documents"
            " (workspace_id, filename, file_size, qdrant_collection_name,"
            "  object_key, folder_id, embedding_fingerprint, status)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, 'PENDING') RETURNING *",
            workspace_id,
            filename,
            file_size,
            qdrant_collection_name,
            object_key,
            folder_id,
            embedding_fingerprint,
        )
        return Document(**dict(row))

    async def get(self, document_id: UUID) -> Document | None:
        """A non-deleted document by id (soft-deleted rows are invisible)."""
        row = await self.conn.fetchrow(
            "SELECT * FROM documents WHERE id = $1 AND deleted_at IS NULL", document_id
        )
        return Document(**dict(row)) if row else None

    async def list_by_workspace(self, workspace_id: UUID) -> list[Document]:
        """All non-deleted documents in a workspace (across every folder)."""
        rows = await self.conn.fetch(
            "SELECT * FROM documents WHERE workspace_id = $1 AND deleted_at IS NULL"
            " ORDER BY created_at DESC",
            workspace_id,
        )
        return [Document(**dict(r)) for r in rows]

    async def list_live_by_workspace(self, workspace_id: UUID) -> list[Document]:
        """Documents that belong in retrieval: neither soft-deleted nor archived
        (the same "live" definition the workers use). Re-index iterates these so
        an archived document is never re-ingested back into the vector store."""
        rows = await self.conn.fetch(
            "SELECT * FROM documents WHERE workspace_id = $1"
            " AND deleted_at IS NULL AND archived_at IS NULL"
            " ORDER BY created_at DESC",
            workspace_id,
        )
        return [Document(**dict(r)) for r in rows]

    async def list_by_folder(self, workspace_id: UUID, folder_id: UUID | None) -> list[Document]:
        """Non-deleted documents directly in one folder (folder_id NULL = root)."""
        if folder_id is None:
            rows = await self.conn.fetch(
                "SELECT * FROM documents WHERE workspace_id = $1"
                " AND folder_id IS NULL AND deleted_at IS NULL"
                " ORDER BY created_at DESC",
                workspace_id,
            )
        else:
            rows = await self.conn.fetch(
                "SELECT * FROM documents WHERE workspace_id = $1"
                " AND folder_id = $2 AND deleted_at IS NULL"
                " ORDER BY created_at DESC",
                workspace_id,
                folder_id,
            )
        return [Document(**dict(r)) for r in rows]

    async def list_by_folder_ids(self, folder_ids: list[UUID]) -> list[Document]:
        """Non-deleted documents directly in any of the given folders. Used by the
        recursive folder delete to enumerate docs whose chunks/objects must be
        purged before the folders are dropped."""
        if not folder_ids:
            return []
        rows = await self.conn.fetch(
            "SELECT * FROM documents WHERE folder_id = ANY($1::uuid[]) AND deleted_at IS NULL",
            folder_ids,
        )
        return [Document(**dict(r)) for r in rows]

    async def move(self, document_id: UUID, folder_id: UUID | None) -> Document | None:
        """Move a doc to a folder (or to root when folder_id is None)."""
        row = await self.conn.fetchrow(
            "UPDATE documents SET folder_id = $2, updated_at = now()"
            " WHERE id = $1 AND deleted_at IS NULL RETURNING *",
            document_id,
            folder_id,
        )
        return Document(**dict(row)) if row else None

    async def archive(self, document_id: UUID) -> Document | None:
        """Mark a doc ARCHIVED (status + archived_at). The Qdrant-chunk purge runs
        out-of-band via the document.deleted cascade (purge_object=false)."""
        row = await self.conn.fetchrow(
            "UPDATE documents SET status = 'ARCHIVED', archived_at = now(),"
            " updated_at = now() WHERE id = $1 AND deleted_at IS NULL RETURNING *",
            document_id,
        )
        return Document(**dict(row)) if row else None

    async def soft_delete(self, document_id: UUID) -> bool:
        """Soft-delete a doc (set deleted_at). The hard purge of Qdrant chunks +
        the MinIO object runs out-of-band via the document.deleted cascade."""
        result = await self.conn.execute(
            "UPDATE documents SET deleted_at = now(), updated_at = now()"
            " WHERE id = $1 AND deleted_at IS NULL",
            document_id,
        )
        return result.endswith("1")

    async def reset_for_reindex(
        self, document_id: UUID, qdrant_collection_name: str, embedding_fingerprint: str
    ) -> None:
        """Point a document at a (new) collection + embedding profile and re-arm it
        for ingestion: status -> PENDING, error (message, code, params) cleared.
        Used by the re-index / re-embed flow when the embedding profile changes."""
        await self.conn.execute(
            "UPDATE documents SET qdrant_collection_name = $2, embedding_fingerprint = $3,"
            " status = 'PENDING', error_message = NULL, error_code = NULL, error_params = NULL,"
            " updated_at = now()"
            " WHERE id = $1 AND deleted_at IS NULL AND archived_at IS NULL",
            document_id,
            qdrant_collection_name,
            embedding_fingerprint,
        )

    async def list_failed(self) -> list[Document]:
        """Every non-deleted document in the FAILED state, platform-wide. Used by
        the manual 'retry-failed' operator override (platform admin) to re-arm +
        re-publish them, bypassing the auto-retry backoff/cap."""
        rows = await self.conn.fetch(
            "SELECT * FROM documents WHERE status = 'FAILED'"
            " AND deleted_at IS NULL AND archived_at IS NULL"
            " ORDER BY created_at DESC"
        )
        return [Document(**dict(r)) for r in rows]

    async def reset_for_retry(
        self, document_id: UUID, qdrant_collection_name: str, embedding_fingerprint: str
    ) -> None:
        """Manual operator override: re-arm a FAILED document for ingestion and
        CLEAR its retry bookkeeping (retry_count -> 0), so the auto-retry sweep's cap
        no longer applies. Like reset_for_reindex it targets the CURRENT embedding
        profile: a retry into a collection the gateway no longer serves would only
        fail again."""
        await self.conn.execute(
            "UPDATE documents SET qdrant_collection_name = $2, embedding_fingerprint = $3,"
            " status = 'PENDING', error_message = NULL, error_code = NULL, error_params = NULL,"
            " retry_count = 0, last_retry_at = now(), updated_at = now()"
            " WHERE id = $1 AND deleted_at IS NULL AND archived_at IS NULL",
            document_id,
            qdrant_collection_name,
            embedding_fingerprint,
        )

    async def list_live_off_fingerprint(self, fingerprint: str) -> list[Document]:
        """Live documents NOT indexed under ``fingerprint`` (the re-embed set)."""
        rows = await self.conn.fetch(
            "SELECT * FROM documents WHERE deleted_at IS NULL AND archived_at IS NULL"
            " AND embedding_fingerprint IS DISTINCT FROM $1 ORDER BY created_at",
            fingerprint,
        )
        return [Document(**dict(r)) for r in rows]

    async def count_live_off_fingerprint(self, fingerprint: str) -> int:
        """Live documents (neither deleted nor archived) NOT indexed under
        ``fingerprint`` — including rows with no fingerprint at all."""
        return await self.conn.fetchval(
            "SELECT count(*) FROM documents"
            " WHERE deleted_at IS NULL AND archived_at IS NULL"
            " AND embedding_fingerprint IS DISTINCT FROM $1",
            fingerprint,
        )

    async def live_counts_by_fingerprint(self) -> list[tuple[str | None, int]]:
        """Live documents per embedding fingerprint, largest group first."""
        rows = await self.conn.fetch(
            "SELECT embedding_fingerprint, count(*) AS n FROM documents"
            " WHERE deleted_at IS NULL AND archived_at IS NULL"
            " GROUP BY embedding_fingerprint ORDER BY n DESC, embedding_fingerprint"
        )
        return [(r["embedding_fingerprint"], r["n"]) for r in rows]

    async def reembed_progress(self, fingerprint: str) -> dict[str, int]:
        """Status breakdown of the live documents targeting ``fingerprint``, plus
        how many live documents are still on another (or no) fingerprint."""
        row = await self.conn.fetchrow(
            "SELECT"
            " count(*) FILTER (WHERE embedding_fingerprint = $1) AS total,"
            " count(*) FILTER (WHERE embedding_fingerprint = $1 AND status = 'SUCCESS')"
            "   AS completed,"
            " count(*) FILTER (WHERE embedding_fingerprint = $1 AND status = 'PENDING')"
            "   AS pending,"
            " count(*) FILTER (WHERE embedding_fingerprint = $1 AND status = 'PROCESSING')"
            "   AS processing,"
            " count(*) FILTER (WHERE embedding_fingerprint = $1 AND status = 'FAILED')"
            "   AS failed,"
            " count(*) FILTER (WHERE embedding_fingerprint IS DISTINCT FROM $1) AS stale"
            " FROM documents WHERE deleted_at IS NULL AND archived_at IS NULL",
            fingerprint,
        )
        return dict(row)

    async def list_by_team_ids(self, team_ids: list[UUID]) -> list[Document]:
        """The team document pool: every document in any workspace assigned to
        one of the given teams (caller passes a team's closure descendants)."""
        if not team_ids:
            return []
        rows = await self.conn.fetch(
            "SELECT d.* FROM documents d"
            " JOIN workspaces w ON w.id = d.workspace_id"
            " WHERE w.team_id = ANY($1::uuid[]) AND d.deleted_at IS NULL"
            " ORDER BY d.created_at DESC",
            team_ids,
        )
        return [Document(**dict(r)) for r in rows]


class DocumentFolderRepo:
    """A workspace's document Drive tree (0017). Self-referential; names unique
    per (workspace, parent) case-insensitively (enforced by a partial-ish unique
    index that collapses a NULL parent to the all-zero UUID)."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self.conn = conn

    async def list_by_workspace(self, workspace_id: UUID) -> list[DocumentFolder]:
        """Every folder in the workspace (flat; the caller builds the tree from
        parent_folder_id)."""
        rows = await self.conn.fetch(
            "SELECT * FROM document_folders WHERE workspace_id = $1"
            " ORDER BY parent_folder_id NULLS FIRST, lower(name)",
            workspace_id,
        )
        return [DocumentFolder(**dict(r)) for r in rows]

    async def get(self, folder_id: UUID) -> DocumentFolder | None:
        row = await self.conn.fetchrow("SELECT * FROM document_folders WHERE id = $1", folder_id)
        return DocumentFolder(**dict(row)) if row else None

    async def create(
        self,
        workspace_id: UUID,
        name: str,
        *,
        parent_folder_id: UUID | None = None,
    ) -> DocumentFolder:
        row = await self.conn.fetchrow(
            "INSERT INTO document_folders (workspace_id, name, parent_folder_id)"
            " VALUES ($1, $2, $3) RETURNING *",
            workspace_id,
            name,
            parent_folder_id,
        )
        return DocumentFolder(**dict(row))

    _UPDATABLE = ("name", "parent_folder_id")

    async def update(self, folder_id: UUID, **fields: object) -> DocumentFolder | None:
        """Dynamic-SET patcher: rename and/or re-parent. parent_folder_id=None moves
        the folder to the workspace root (passing it explicitly nulls the column)."""
        sets, vals = patch_set(self._UPDATABLE, fields, [folder_id])
        if not sets:
            return await self.get(folder_id)
        row = await self.conn.fetchrow(
            compose_sql(
                "UPDATE document_folders SET {sets}, updated_at = now() WHERE id = $1 RETURNING *",
                sets=sets,
            ),
            *vals,
        )
        return DocumentFolder(**dict(row)) if row else None

    async def descendant_ids(self, folder_id: UUID) -> list[UUID]:
        """The folder itself plus every descendant folder (recursive CTE), so a
        recursive delete can enumerate the whole subtree of folders and their
        documents before dropping it."""
        rows = await self.conn.fetch(
            "WITH RECURSIVE subtree AS ("
            "   SELECT id FROM document_folders WHERE id = $1"
            "   UNION ALL"
            "   SELECT f.id FROM document_folders f"
            "     JOIN subtree s ON f.parent_folder_id = s.id"
            " ) SELECT id FROM subtree",
            folder_id,
        )
        return [r["id"] for r in rows]

    async def delete(self, folder_id: UUID) -> bool:
        """Delete a folder. The ON DELETE CASCADE FK drops child folders too; docs
        in those folders are detached (documents.folder_id ON DELETE SET NULL) —
        the API soft-deletes them explicitly first so they don't leak to root."""
        result = await self.conn.execute("DELETE FROM document_folders WHERE id = $1", folder_id)
        return result.endswith("1")
