"""测试夹具：连接真实 PostgreSQL（默认本机 hvtest_test 库），不使用任何假存储。

通过 TEST_DATABASE_URL 指向任意真实 PostgreSQL；数据库不存在时会自动创建。
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from sqlalchemy import text

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://hvtest:hvtest@127.0.0.1:5432/hvtest_test",
)

# 必须在导入 app 之前设置：app 的数据库引擎在导入时读取该变量。
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


def _ensure_database(url: str) -> None:
    u = sa.engine.make_url(url)
    maintenance = u.set(database="postgres")
    engine = sa.create_engine(maintenance, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": u.database},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{u.database}"'))
    finally:
        engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _database():
    _ensure_database(TEST_DATABASE_URL)
    from app.db import Base, engine

    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _clean_tables(_database):
    from app.db import engine

    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE benches"))
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
