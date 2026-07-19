"""L6: assignment list fields must reject unbounded payloads.

`AssignmentUpdate.tenant_ids` and `BulkAssignmentUpdate.user_ids`/`tenant_ids` used to
accept a list of any length -- a caller (or a bug on the client side) could submit a
gigantic list and force the server to build the corresponding SQL `IN (...)` clause
against it. `max_length` on the `Field` gives Pydantic a hard, cheap-to-check ceiling
before the request body is ever handed to a repository. Pure schema-level test -- no
HTTP/DB needed, this is exactly where the resulting `422` originates.
"""

from __future__ import annotations

import pytest
from app.schemas.assignment import AssignmentUpdate, BulkAssignmentUpdate
from pydantic import ValidationError


def test_assignment_update_tenant_ids_over_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        AssignmentUpdate(tenant_ids=list(range(501)))


def test_assignment_update_tenant_ids_within_cap_accepted() -> None:
    update = AssignmentUpdate(tenant_ids=list(range(500)))
    assert len(update.tenant_ids) == 500


def test_assignment_update_default_tenant_ids_still_empty() -> None:
    assert AssignmentUpdate().tenant_ids == []


def test_bulk_assignment_update_user_ids_over_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        BulkAssignmentUpdate(user_ids=list(range(2001)), tenant_ids=[1], action="add")


def test_bulk_assignment_update_user_ids_within_cap_accepted() -> None:
    update = BulkAssignmentUpdate(user_ids=list(range(2000)), tenant_ids=[1], action="add")
    assert len(update.user_ids) == 2000


def test_bulk_assignment_update_tenant_ids_over_cap_rejected() -> None:
    with pytest.raises(ValidationError):
        BulkAssignmentUpdate(user_ids=[1], tenant_ids=list(range(501)), action="add")


def test_bulk_assignment_update_tenant_ids_within_cap_accepted() -> None:
    update = BulkAssignmentUpdate(user_ids=[1], tenant_ids=list(range(500)), action="add")
    assert len(update.tenant_ids) == 500
