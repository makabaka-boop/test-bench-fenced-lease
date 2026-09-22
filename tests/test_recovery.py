"""State survives an API restart and (optionally) a database restart."""
import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from conftest import acquire, creds, register


async def _new_client(dsn: str):
    # A fresh app object + fresh connection pool simulates a restarted API
    # process; schema creation is idempotent.
    from app.main import create_app

    application = create_app(dsn)
    transport = ASGITransport(app=application)
    ac = AsyncClient(transport=transport, base_url="http://test")
    lifespan = application.router.lifespan_context(application)
    await lifespan.__aenter__()
    return ac, application, lifespan


@pytest.mark.asyncio
async def test_state_survives_api_restart(client, pool, database_url, bench_name):
    await register(client, bench_name, lo=0, hi=1000, initial=250)
    held = await acquire(client, bench_name, "engineer-A", duration=300)
    a = creds(held, "engineer-A")
    gen = a["fence_generation"]
    await client.post(
        f"/benches/{bench_name}/setpoint", json={**a, "setpoint": 815}
    )

    # Simulate the API process restarting: new app object, new pool, schema
    # creation is idempotent. The database (and its volume) keep running.
    ac2, application2, lifespan2 = await _new_client(database_url)
    try:
        state = (await ac2.get(f"/benches/{bench_name}")).json()
        assert state["current_setpoint"] == 815
        assert state["leased"] is True
        assert state["lease"]["holder"] == "engineer-A"
        assert state["lease"]["fence_generation"] == gen

        # The same lease/fence still authorizes writes after restart.
        r = await ac2.post(
            f"/benches/{bench_name}/setpoint", json={**a, "setpoint": 123}
        )
        assert r.status_code == 200
        assert r.json()["current_setpoint"] == 123
    finally:
        await ac2.aclose()
        await lifespan2.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_free_bench_stays_free_across_restart(client, pool, database_url, bench_name):
    await register(client, bench_name)
    ac2, application2, lifespan2 = await _new_client(database_url)
    try:
        state = (await ac2.get(f"/benches/{bench_name}")).json()
        assert state["leased"] is False
        assert state["lease"] is None

        held = await acquire(ac2, bench_name, "engineer-Z", duration=10)
        assert held["lease"]["holder"] == "engineer-Z"
        assert held["leased"] is True
    finally:
        await ac2.aclose()
        await lifespan2.__aexit__(None, None, None)
