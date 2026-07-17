"""tenant is_default and local account home

Revision ID: 4035552093e2
Revises: cd755854e58c
Create Date: 2026-07-17 15:23:40.281135
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4035552093e2"
down_revision: str | None = "cd755854e58c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) Neue Spalte -- die eine instanzweite Heim-Tenant-Kennzeichnung, unabhängig vom Slug
    # (der Slug bleibt umbenennbar, siehe Task 2). server_default schützt bestehende Zeilen
    # (alle zunächst `false`), bevor Schritt 2 den Incumbent markiert.
    op.add_column(
        "tenant",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # 2) Den bisherigen Default-Tenant (identifiziert über seinen bis hierhin garantiert noch
    # unveränderten Slug) als `is_default` markieren -- das letzte Mal, dass `slug = 'default'`
    # in diesem Codepfad korrekt ist: hier wird die alte Slug-Identität in die neue,
    # slug-unabhängige Kennzeichnung überführt.
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE tenant SET is_default = true WHERE slug = 'default'"))

    # 3) Höchstens EIN Default-Tenant -- partieller Unique-Index statt einer normalen
    # Unique-Constraint auf der ganzen Spalte, da sonst auch nur eine einzige `false`-Zeile
    # erlaubt wäre. Schritt 2 (+ jedes künftige Setup/Seed) hält bereits GENAU eine Zeile auf
    # `true`; dieser Index macht eine zweite schlicht unmöglich (DB-Ebene, kein Race möglich).
    op.create_index(
        "uq_tenant_is_default",
        "tenant",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    # 4) Heim-Tenant für bestehende lokale Konten ohne eigene Kunden-Zuordnung nachtragen:
    # Superadmin/Admin/Auditor sind Provider-Personal und werden auf den Default-Tenant
    # geheimatet (Design §2). SSO-Konten tragen bereits ihre `tenant_id` (aus dem tid-Claim)
    # und bleiben unangetastet -- der Filter `tenant_id IS NULL` trifft ohnehin nur lokale
    # Konten ohne Heimat.
    conn.execute(
        sa.text(
            "UPDATE app_user SET tenant_id = (SELECT id FROM tenant WHERE is_default) "
            "WHERE is_sso = false AND tenant_id IS NULL"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    # Symmetrisch in umgekehrter Reihenfolge: erst den Heim-Tenant-Backfill zurücknehmen
    # (lokale Konten wieder auf NULL, den Vor-Migration-Zustand), dann den Unique-Index,
    # dann die Spalte selbst.
    conn.execute(sa.text("UPDATE app_user SET tenant_id = NULL WHERE is_sso = false"))
    op.drop_index("uq_tenant_is_default", table_name="tenant")
    op.drop_column("tenant", "is_default")
