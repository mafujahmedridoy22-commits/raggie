# SPDX-License-Identifier: Apache-2.0

"""The one 404 for a resource that is missing or outside the caller's scope.

Every "not found" response went through a hand-written
``HTTPException(status.HTTP_404_NOT_FOUND, f"{resource} not found")``, so the
convention (status, wording, and the deliberate policy of making a foreign id
indistinguishable from a missing one) lived in ~55 copies with nothing linking
them. This is the single place it is defined, so it changes once.
"""

from __future__ import annotations

from fastapi import HTTPException, status


def not_found(resource: str) -> HTTPException:
    """A 404 for ``resource`` (e.g. ``not_found("workspace")``). An id that
    belongs to another tenant is reported exactly like one that does not exist,
    so the caller can never probe for the presence of another tenant's rows."""
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{resource} not found")
