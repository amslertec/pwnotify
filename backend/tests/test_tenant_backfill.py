from sqlalchemy import text

# Hinweis: die Notification-Tabelle heißt physisch `notification_log`
# (siehe app/models/notification.py, __tablename__ = "notification_log").
DATA_TABLES = ["entra_user", "exclusion", "notification_log", "run", "setting", "audit_log"]


async def test_all_data_tables_have_tenant_id(session):
    for tbl in DATA_TABLES:
        col = (
            await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = 'tenant_id'"
                ),
                {"t": tbl},
            )
        ).scalar_one_or_none()
        assert col == "tenant_id", f"{tbl} fehlt tenant_id"


async def test_default_tenant_exists(session):
    slug = (
        await session.execute(text("SELECT slug FROM tenant WHERE slug = 'default'"))
    ).scalar_one_or_none()
    assert slug == "default"


async def test_setting_primary_key_is_composite(session):
    cols = (
        (
            await session.execute(
                text(
                    "SELECT a.attname FROM pg_index i "
                    "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
                    "WHERE i.indrelid = 'setting'::regclass AND i.indisprimary ORDER BY a.attname"
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(cols) == {"tenant_id", "key"}
