"""Pytest fixtures backed by a real PostgreSQL instance.

Tests always talk to a real, persistent PostgreSQL server addressed by
``DATABASE_URL`` (set in docker-compose for the containerised run, or to a
locally running cluster for development). No storage is mocked and no
response is faked: schema, row locks and database time all come from
PostgreSQL.

Each test creates uniquely named benches, so data may safely persist on the
volume between cases (which also lets the restart/recovery test work).
"""
from __future__ import annotations

import os

import asyncpg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import create_app


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL must point at a real PostgreSQL instance "
            "(docker compose run api-tests, or a local cluster)"
        )
    return url


@pytest.fixture(scope="session")
def database_url() -> str:
    return _database_url()


@pytest_asyncio.fixture
async def pool(database_url):
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=10)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def client(database_url):
    """One API instance + httpx client per test."""
    app = create_app(database_url)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Run lifespan startup explicitly (creates the schema).
        async with app.router.lifespan_context(app):
            yield ac


@pytest.fixture
def bench_name() -> str:
    # Unique per test to keep the persistent volume clean between cases.
    return f"bench-{os.urandom(6).hex()}"


async def register(client, name, lo=0, hi=1000, initial=0):
    r = await client.post(
        "/benches",
        json={
            "name": name,
            "min_setpoint": lo,
            "max_setpoint": hi,
            "initial_setpoint": initial,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def acquire(client, name, holder, duration=30):
    r = await client.post(
        f"/benches/{name}/lease",
        json={"holder": holder, "duration_seconds": duration},
    )
    assert r.status_code == 200, r.text
    return r.json()


def creds(status_body, holder):
    lease = status_body["lease"]
    return {
        "holder": holder,
        "lease_token": lease["lease_token"],
        "fence_generation": lease["fence_generation"],
    }
