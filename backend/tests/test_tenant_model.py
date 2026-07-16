from app.models.tenant import AuditorTenant, Tenant
from app.models.user import AppUser
from sqlalchemy import select


async def test_create_tenant_and_bind_local_auditor(session):
    t = Tenant(
        name="Contoso AG",
        slug="contoso",
        entra_tenant_id="00000000-aaaa-bbbb-cccc-111111111111",
    )
    session.add(t)
    await session.flush()
    assert t.id is not None and t.is_active is True

    auditor = AppUser(username="lokal-auditor@example.com", password_hash="x", role="auditor")
    session.add(auditor)
    await session.flush()

    session.add(AuditorTenant(user_id=auditor.id, tenant_id=t.id))
    await session.flush()

    bound = (
        (await session.execute(select(AuditorTenant).where(AuditorTenant.user_id == auditor.id)))
        .scalars()
        .all()
    )
    assert [b.tenant_id for b in bound] == [t.id]


async def test_sso_user_carries_single_tenant_id(session):
    t = Tenant(name="Fabrikam", slug="fabrikam")
    session.add(t)
    await session.flush()
    sso = AppUser(
        username="admin@fabrikam.de",
        password_hash="x",
        role="admin",
        is_sso=True,
        tenant_id=t.id,
    )
    session.add(sso)
    await session.flush()
    assert sso.tenant_id == t.id
