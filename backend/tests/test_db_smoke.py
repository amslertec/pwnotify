from sqlalchemy import text


async def test_test_database_is_reachable_and_migrated(session):
    # app_user existiert dank bestehender Migrationen -> Migration lief gegen die Test-DB.
    rows = (await session.execute(text("SELECT to_regclass('public.app_user')"))).scalar_one()
    assert rows == "app_user"
