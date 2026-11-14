# SPDX-License-Identifier: Apache-2.0

"""Document upload intake (producer side of ingestion).

POST stores the raw file in the object store, creates a PENDING documents row,
and publishes raggie.document.uploaded for the ingestion workers (contract in
contracts/events.md). Every intake path targets the collection + embedding
fingerprint the embeddings gateway names (embeddings_profile.py) and is refused
while that profile is unknown (503) or no longer matches the indexed data (409).
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field

from raggie_control_plane.api.deps import (
    get_conn,
    get_embeddings_profile,
    get_event_publisher,
    get_object_store,
    ingest_target,
    require_system,
    require_workspace,
)
from raggie_control_plane.api.errors import not_found
from raggie_control_plane.authz import Action
from raggie_control_plane.config import settings
from raggie_control_plane.db.repositories import (
    DocumentFolderRepo,
    DocumentRepo,
    WorkspaceRepo,
)
from raggie_control_plane.embeddings_profile import (
    EmbeddingsProfile,
    IngestTarget,
    reindex_workspace,
    uploaded_event,
)
from raggie_control_plane.infra.objectstore import ObjectStore
from raggie_control_plane.infra.publisher import (
    DOCUMENT_DELETED,
    DOCUMENT_UPLOADED,
    EventPublisher,
)
from raggie_control_plane.models import Document, DocumentFolder, Workspace

# Strip path separators / control chars from a client filename before it becomes
# part of an object key (defense-in-depth against traversal into another prefix).
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._()\-]+")
_DOT_RUN = re.compile(r"\.{2,}")
_SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".webp",
    ".bmp",
    ".txt",
    ".md",
}


def _safe_filename(name: str) -> str:
    base = (name or "upload").replace("\\", "/").split("/")[-1].strip()
    base = _SAFE_NAME.sub("_", base)
    # No "." / ".." components or dot-files: collapse dot runs, drop leading dots.
    base = _DOT_RUN.sub(".", base).lstrip(".")
    return base[:200].rstrip(".") or "upload"


def _validated_filename(name: str) -> str:
    filename = _safe_filename(name)
    if Path(filename).suffix.lower() not in _SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "unsupported document type",
        )
    return filename


async def _read_upload_limited(file: UploadFile, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"direct upload exceeds the {max_bytes} byte limit; use resumable upload",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_request_limited(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    f"upload part exceeds the {max_bytes} byte limit",
                )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid content-length") from exc

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"upload part exceeds the {max_bytes} byte limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


router = APIRouter(prefix="/api/v1/admin", tags=["documents"])


async def _publish_document_deleted(
    publisher: EventPublisher, doc: Document, *, purge_object: bool
) -> None:
    """Emit raggie.document.deleted so the deletion-cascade worker drops the doc's
    Qdrant chunks (and, when purge_object, hard-deletes the MinIO object). The
    collection is pinned from the doc so the worker need not re-derive it. The
    contract lives in contracts/events.md / raggie_workers.ingestion.deletion."""
    await publisher.publish(
        DOCUMENT_DELETED,
        {
            "document_id": str(doc.id),
            "workspace_id": str(doc.workspace_id),
            "object_key": doc.object_key,
            "collection": doc.qdrant_collection_name,
            "purge_object": purge_object,
        },
    )


async def _validate_folder(
    conn: asyncpg.Connection, folder_id: UUID | None, workspace_id: UUID
) -> None:
    """A folder_id (when given) must exist and belong to this workspace, so a doc
    can't be parked in another workspace's folder."""
    if folder_id is None:
        return
    folder = await DocumentFolderRepo(conn).get(folder_id)
    if folder is None or folder.workspace_id != workspace_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "folder not in workspace")


@router.post(
    "/workspaces/{workspace_id}/documents/reindex",
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def reindex_workspace_documents(
    workspace_id: UUID,
    conn: asyncpg.Connection = Depends(get_conn),
    publisher: EventPublisher = Depends(get_event_publisher),
    profile: EmbeddingsProfile = Depends(get_embeddings_profile),
) -> dict[str, int]:
    """Re-embed a workspace's live documents into the gateway's current
    collection (e.g. after a failed batch). Point ids are deterministic."""
    workspace = await WorkspaceRepo(conn).get(workspace_id)
    if workspace is None:
        raise not_found("workspace")
    target = await ingest_target(profile, conn)
    return await reindex_workspace(conn, publisher, workspace, target)


@router.post(
    "/documents/reindex-all",
    dependencies=[Depends(require_system())],
)
async def reindex_all_documents(
    conn: asyncpg.Connection = Depends(get_conn),
    publisher: EventPublisher = Depends(get_event_publisher),
    profile: EmbeddingsProfile = Depends(get_embeddings_profile),
) -> dict[str, int]:
    """Platform-wide re-index of every live document into the gateway's current
    collection (super-admin). Refused on an embedding-profile mismatch: moving to
    a new profile is POST /embeddings/reembed, which also records the switch.
    Safe to re-run (idempotent point ids)."""
    target = await ingest_target(profile, conn)
    workspaces = await WorkspaceRepo(conn).list_all()
    total = {"reindexed": 0, "skipped": 0, "workspaces": len(workspaces)}
    for ws in workspaces:
        res = await reindex_workspace(conn, publisher, ws, target)
        total["reindexed"] += res["reindexed"]
        total["skipped"] += res["skipped"]
    return total


@router.post(
    "/documents/retry-failed",
    dependencies=[Depends(require_system())],
)
async def retry_failed_documents(
    conn: asyncpg.Connection = Depends(get_conn),
    publisher: EventPublisher = Depends(get_event_publisher),
    profile: EmbeddingsProfile = Depends(get_embeddings_profile),
) -> dict[str, int]:
    """Manual 'retry now' override (super-admin): re-arm + re-publish EVERY FAILED
    document, ignoring the auto-retry sweep's exponential backoff AND its max_retries
    cap. retry_count is RESET to 0 so the sweep treats each doc as fresh afterwards.
    Reuses the same republish path (document.uploaded) as reindex-all, targeting
    the gateway's current collection, so point ids stay deterministic/idempotent.
    Returns {retried: <n>}."""
    target = await ingest_target(profile, conn)
    repo = DocumentRepo(conn)
    docs = await repo.list_failed()
    retried = 0
    for doc in docs:
        if not doc.object_key:
            continue  # no raw file retained → cannot re-ingest
        await repo.reset_for_retry(doc.id, target.collection, target.fingerprint)
        await publisher.publish(DOCUMENT_UPLOADED, uploaded_event(doc, target))
        retried += 1
    return {"retried": retried}


@router.post(
    "/workspaces/{workspace_id}/documents",
    response_model=Document,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def upload_document(
    workspace_id: UUID,
    file: UploadFile = File(...),
    folder_id: UUID | None = Form(None),  # optional Drive folder to land the file in
    conn: asyncpg.Connection = Depends(get_conn),
    store: ObjectStore = Depends(get_object_store),
    publisher: EventPublisher = Depends(get_event_publisher),
    profile: EmbeddingsProfile = Depends(get_embeddings_profile),
) -> Document:
    workspace = await WorkspaceRepo(conn).get(workspace_id)
    if workspace is None:
        raise not_found("workspace")
    await _validate_folder(conn, folder_id, workspace_id)
    target = await ingest_target(profile, conn)

    max_bytes = min(settings.direct_upload_max_bytes, settings.max_upload_bytes)
    data = await _read_upload_limited(file, max_bytes)
    filename = _validated_filename(file.filename or "upload")
    return await ingest_document(
        conn,
        store,
        publisher,
        workspace,
        filename,
        data,
        target=target,
        content_type=file.content_type,
        folder_id=folder_id,
    )


async def ingest_document(
    conn: asyncpg.Connection,
    store: ObjectStore,
    publisher: EventPublisher,
    workspace: Workspace,
    filename: str,
    data: bytes,
    *,
    target: IngestTarget,
    content_type: str | None = None,
    folder_id: UUID | None = None,
) -> Document:
    """The single intake path: store the raw object, create the PENDING row and
    publish document.uploaded for the ingestion workers. ``filename`` must
    already be validated (``_validated_filename``); ``target`` comes from the
    embedding profile. Shared by the upload route and the demo seeder."""
    object_key = f"uploads/{workspace.id}/{uuid4().hex}-{filename}"
    await store.put(object_key, data, content_type or "application/octet-stream")

    document = await DocumentRepo(conn).create(
        workspace.id,
        filename,
        len(data),
        target.collection,
        object_key=object_key,
        folder_id=folder_id,
        embedding_fingerprint=target.fingerprint,
    )
    await publisher.publish(DOCUMENT_UPLOADED, uploaded_event(document, target))
    return document


@router.get(
    "/workspaces/{workspace_id}/documents",
    response_model=list[Document],
    dependencies=[Depends(require_workspace(Action.VIEW_WORKSPACE))],
)
async def list_documents(
    workspace_id: UUID,
    folder_id: str | None = Query(
        None,
        description=(
            "Filter to one folder. 'root' (or empty) = docs at the workspace root"
            " (folder_id IS NULL); a UUID = that folder; omitted = all non-deleted"
            " docs across every folder."
        ),
    ),
    conn: asyncpg.Connection = Depends(get_conn),
) -> list[Document]:
    repo = DocumentRepo(conn)
    if folder_id is None:
        # No filter: every non-deleted doc in the workspace (back-compat).
        return await repo.list_by_workspace(workspace_id)
    if folder_id in ("root", ""):
        return await repo.list_by_folder(workspace_id, None)
    try:
        parsed = UUID(folder_id)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "folder_id must be a UUID or 'root'"
        ) from exc
    return await repo.list_by_folder(workspace_id, parsed)


# ─────────────────────── Document lifecycle + move ───────────────────────


async def _load_workspace_doc(
    conn: asyncpg.Connection, workspace_id: UUID, document_id: UUID
) -> Document:
    """Fetch a non-deleted document and assert it belongs to this workspace."""
    doc = await DocumentRepo(conn).get(document_id)
    if doc is None or doc.workspace_id != workspace_id or doc.deleted_at is not None:
        raise not_found("document")
    return doc


@router.post(
    "/workspaces/{workspace_id}/documents/{document_id}/archive",
    response_model=Document,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def archive_document(
    workspace_id: UUID,
    document_id: UUID,
    conn: asyncpg.Connection = Depends(get_conn),
    publisher: EventPublisher = Depends(get_event_publisher),
) -> Document:
    """Archive a document: status -> ARCHIVED + archived_at, and purge its Qdrant
    chunks (purge_object=false) so it stops surfacing in retrieval while the raw
    object stays, keeping the archive reversible."""
    await _load_workspace_doc(conn, workspace_id, document_id)
    archived = await DocumentRepo(conn).archive(document_id)
    if archived is None:
        raise not_found("document")
    await _publish_document_deleted(publisher, archived, purge_object=False)
    return archived


@router.delete(
    "/workspaces/{workspace_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def delete_document(
    workspace_id: UUID,
    document_id: UUID,
    conn: asyncpg.Connection = Depends(get_conn),
    publisher: EventPublisher = Depends(get_event_publisher),
) -> None:
    """Soft-delete a document (set deleted_at) and publish the hard-purge cascade
    (purge_object=true): the worker drops the Qdrant chunks AND the MinIO object."""
    doc = await _load_workspace_doc(conn, workspace_id, document_id)
    if not await DocumentRepo(conn).soft_delete(document_id):
        raise not_found("document")
    await _publish_document_deleted(publisher, doc, purge_object=True)


class DocumentMovePatch(BaseModel):
    # Explicit null = move to the workspace root. Required so a client can null it.
    folder_id: UUID | None = None


@router.patch(
    "/workspaces/{workspace_id}/documents/{document_id}",
    response_model=Document,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def move_document(
    workspace_id: UUID,
    document_id: UUID,
    body: DocumentMovePatch,
    conn: asyncpg.Connection = Depends(get_conn),
) -> Document:
    """Move a document into a folder (or to the root when folder_id is null)."""
    await _load_workspace_doc(conn, workspace_id, document_id)
    await _validate_folder(conn, body.folder_id, workspace_id)
    moved = await DocumentRepo(conn).move(document_id, body.folder_id)
    if moved is None:
        raise not_found("document")
    return moved


# ─────────────────────────────── Folders (Drive) ───────────────────────────


class FolderIn(BaseModel):
    name: str
    parent_folder_id: UUID | None = None


class FolderPatch(BaseModel):
    name: str | None = None
    parent_folder_id: UUID | None = None


def _clean_folder_name(name: str) -> str:
    cleaned = " ".join((name or "").split()).strip()
    if not cleaned:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name required")
    return cleaned[:200]


async def _assert_parent_in_workspace(
    conn: asyncpg.Connection, parent_id: UUID | None, workspace_id: UUID
) -> None:
    if parent_id is None:
        return
    parent = await DocumentFolderRepo(conn).get(parent_id)
    if parent is None or parent.workspace_id != workspace_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "parent folder not in workspace")


@router.get(
    "/workspaces/{workspace_id}/folders",
    response_model=list[DocumentFolder],
    dependencies=[Depends(require_workspace(Action.VIEW_WORKSPACE))],
)
async def list_folders(
    workspace_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> list[DocumentFolder]:
    """Flat list of every folder in the workspace (the web builds the tree from
    parent_folder_id)."""
    return await DocumentFolderRepo(conn).list_by_workspace(workspace_id)


@router.post(
    "/workspaces/{workspace_id}/folders",
    response_model=DocumentFolder,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def create_folder(
    workspace_id: UUID,
    body: FolderIn,
    conn: asyncpg.Connection = Depends(get_conn),
) -> DocumentFolder:
    workspace = await WorkspaceRepo(conn).get(workspace_id)
    if workspace is None:
        raise not_found("workspace")
    name = _clean_folder_name(body.name)
    await _assert_parent_in_workspace(conn, body.parent_folder_id, workspace_id)
    try:
        return await DocumentFolderRepo(conn).create(
            workspace_id, name, parent_folder_id=body.parent_folder_id
        )
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "folder already exists") from exc


@router.patch(
    "/workspaces/{workspace_id}/folders/{folder_id}",
    response_model=DocumentFolder,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def update_folder(
    workspace_id: UUID,
    folder_id: UUID,
    body: FolderPatch,
    conn: asyncpg.Connection = Depends(get_conn),
) -> DocumentFolder:
    """Rename and/or re-parent (move) a folder."""
    repo = DocumentFolderRepo(conn)
    folder = await repo.get(folder_id)
    if folder is None or folder.workspace_id != workspace_id:
        raise not_found("folder")
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and fields["name"] is not None:
        fields["name"] = _clean_folder_name(fields["name"])
    if "parent_folder_id" in fields and fields["parent_folder_id"] is not None:
        new_parent = fields["parent_folder_id"]
        if new_parent == folder_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "folder cannot be its own parent")
        await _assert_parent_in_workspace(conn, new_parent, workspace_id)
        # No cycles: a folder may not be re-parented under its own descendant.
        if new_parent in set(await repo.descendant_ids(folder_id)):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "cannot move a folder into its own subtree"
            )
    try:
        updated = await repo.update(folder_id, **fields)
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "folder already exists") from exc
    if updated is None:
        raise not_found("folder")
    return updated


@router.delete(
    "/workspaces/{workspace_id}/folders/{folder_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def delete_folder(
    workspace_id: UUID,
    folder_id: UUID,
    conn: asyncpg.Connection = Depends(get_conn),
    publisher: EventPublisher = Depends(get_event_publisher),
) -> None:
    """Recursively delete a folder: soft-delete every document in the folder and
    all descendant folders (publishing the hard-purge cascade per doc), then drop
    the folder subtree (ON DELETE CASCADE removes the child folders)."""
    folder_repo = DocumentFolderRepo(conn)
    doc_repo = DocumentRepo(conn)
    folder = await folder_repo.get(folder_id)
    if folder is None or folder.workspace_id != workspace_id:
        raise not_found("folder")

    # Enumerate the whole subtree of folders, then every non-deleted doc within it.
    subtree_ids = await folder_repo.descendant_ids(folder_id)
    docs = await doc_repo.list_by_folder_ids(subtree_ids)
    for doc in docs:
        if await doc_repo.soft_delete(doc.id):
            await _publish_document_deleted(publisher, doc, purge_object=True)

    # Drop the folder; the FK cascade removes descendant folders.
    await folder_repo.delete(folder_id)


# ─────────────────────── Resumable multipart upload ───────────────────────
# For very large files (GB-scale). The browser splits the file into parts and
# uploads them through the control-plane to S3/MinIO multipart; nothing buffers
# the whole file. The session is stateless server-side: the client holds the
# upload_id + per-part ETags and returns them at /complete. The documents row is
# created (and ingestion published) only on completion, so an abandoned upload
# leaves at most an orphan multipart (no document, no ingestion).


class CreateUploadIn(BaseModel):
    filename: str
    content_type: str | None = None
    size: int | None = Field(None, ge=0)  # total bytes, if known (for an early size-limit check)


class CreateUploadOut(BaseModel):
    upload_id: str
    object_key: str
    part_size: int
    max_bytes: int


class UploadPartOut(BaseModel):
    part_number: int
    etag: str


class CompletePart(BaseModel):
    part_number: int
    etag: str


class CompleteUploadIn(BaseModel):
    upload_id: str
    object_key: str
    filename: str
    parts: list[CompletePart]
    folder_id: UUID | None = None  # optional Drive folder to land the file in


class AbortUploadIn(BaseModel):
    upload_id: str
    object_key: str


def _assert_key_in_workspace(object_key: str, workspace_id: UUID) -> None:
    """The object_key is echoed back by the client across part/complete calls;
    pin it to this workspace's prefix so a caller can't write into another."""
    prefix = f"uploads/{workspace_id}/"
    suffix = object_key.removeprefix(prefix)
    if (
        not object_key.startswith(prefix)
        or not suffix
        or "/" in suffix
        or suffix in {".", ".."}
        or ".mpu" in suffix
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "object_key outside workspace")


@router.post(
    "/workspaces/{workspace_id}/documents/uploads",
    response_model=CreateUploadOut,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def create_upload(
    workspace_id: UUID,
    body: CreateUploadIn,
    conn: asyncpg.Connection = Depends(get_conn),
    store: ObjectStore = Depends(get_object_store),
    profile: EmbeddingsProfile = Depends(get_embeddings_profile),
) -> CreateUploadOut:
    workspace = await WorkspaceRepo(conn).get(workspace_id)
    if workspace is None:
        raise not_found("workspace")
    # Refuse up front rather than after gigabytes of parts.
    await ingest_target(profile, conn)
    if body.size is not None and body.size > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"file exceeds the {settings.max_upload_bytes} byte limit",
        )
    name = _validated_filename(body.filename)
    object_key = f"uploads/{workspace_id}/{uuid4().hex[:8]}-{name}"
    upload_id = await store.create_multipart(
        object_key, body.content_type or "application/octet-stream"
    )
    return CreateUploadOut(
        upload_id=upload_id,
        object_key=object_key,
        part_size=settings.upload_part_size,
        max_bytes=settings.max_upload_bytes,
    )


@router.put(
    "/workspaces/{workspace_id}/documents/uploads/parts",
    response_model=UploadPartOut,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def upload_part(
    workspace_id: UUID,
    request: Request,
    upload_id: str = Header(..., alias="X-Upload-Id"),
    object_key: str = Header(..., alias="X-Object-Key"),
    part_number: int = Header(..., alias="X-Part-Number"),
    store: ObjectStore = Depends(get_object_store),
) -> UploadPartOut:
    _assert_key_in_workspace(object_key, workspace_id)
    if part_number < 1 or part_number > 10000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid part number")
    chunk = await _read_request_limited(request, settings.upload_part_size)
    if not chunk:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty part")
    etag = await store.upload_part(object_key, upload_id, part_number, chunk)
    return UploadPartOut(part_number=part_number, etag=etag)


@router.post(
    "/workspaces/{workspace_id}/documents/uploads/complete",
    response_model=Document,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def complete_upload(
    workspace_id: UUID,
    body: CompleteUploadIn,
    conn: asyncpg.Connection = Depends(get_conn),
    store: ObjectStore = Depends(get_object_store),
    publisher: EventPublisher = Depends(get_event_publisher),
    profile: EmbeddingsProfile = Depends(get_embeddings_profile),
) -> Document:
    _assert_key_in_workspace(body.object_key, workspace_id)
    workspace = await WorkspaceRepo(conn).get(workspace_id)
    if workspace is None:
        raise not_found("workspace")
    if not body.parts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no parts")
    part_numbers = [part.part_number for part in body.parts]
    if (
        len(part_numbers) > 10000
        or len(set(part_numbers)) != len(part_numbers)
        or any(number < 1 or number > 10000 for number in part_numbers)
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid multipart part list")
    await _validate_folder(conn, body.folder_id, workspace_id)
    target = await ingest_target(profile, conn)
    filename = _validated_filename(body.filename)
    if Path(filename).suffix.lower() != Path(body.object_key).suffix.lower():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "filename does not match the upload object",
        )

    size = await store.complete_multipart(
        body.object_key,
        body.upload_id,
        [(p.part_number, p.etag) for p in body.parts],
    )
    if size > settings.max_upload_bytes:
        # Defense in depth: a client could under-report size at create time.
        await store.remove(body.object_key)
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "file exceeds the limit")
    document = await DocumentRepo(conn).create(
        workspace_id,
        filename,
        size,
        target.collection,
        object_key=body.object_key,
        folder_id=body.folder_id,
        embedding_fingerprint=target.fingerprint,
    )
    await publisher.publish(DOCUMENT_UPLOADED, uploaded_event(document, target))
    return document


@router.post(
    "/workspaces/{workspace_id}/documents/uploads/abort",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_workspace(Action.MANAGE_WORKSPACE))],
)
async def abort_upload(
    workspace_id: UUID,
    body: AbortUploadIn,
    store: ObjectStore = Depends(get_object_store),
) -> None:
    _assert_key_in_workspace(body.object_key, workspace_id)
    await store.abort_multipart(body.object_key, body.upload_id)
