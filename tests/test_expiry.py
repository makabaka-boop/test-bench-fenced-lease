"""Expiry is judged from database time only; renewals recompute from DB time."""
import asyncio
from datetime import datetime

import pytest

from conftest import acquire, creds, register


@pytest.mark.asyncio
async def test_takeover_after_expiry_and_old_holder_locked_out(
    client, pool, bench_name
):
    await register(client, bench_name, lo=0, hi=1000, initial=10)
    held_a = await acquire(client, bench_name, "engineer-A", duration=5)
    a = creds(held_a, "engineer-A")
    gen_a = a["fence_generation"]

    # Simulate time passing at the database.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE benches SET expires_at = now() - interval '10 seconds' "
            "WHERE name = $1",
            bench_name,
        )

    # Expired lease shows as free; token is no longer exposed.
    state = (await client.get(f"/benches/{bench_name}")).json()
    assert state["leased"] is False
    assert state["lease"] is None

    # Old holder cannot renew / release / write once expired.
    for path, payload in (
        ("renew", {**a, "duration_seconds": 30}),
        ("release", a),
        ("setpoint", {**a, "setpoint": 500}),
    ):
        r = await client.post(f"/benches/{bench_name}/{path}", json=payload)
        assert r.status_code == 409, (path, r.text)
        assert r.json()["error"]["code"] == "LEASE_EXPIRED", path

    # Competitor takes over; generation advances exactly once.
    held_b = await acquire(client, bench_name, "engineer-B", duration=30)
    b = creds(held_b, "engineer-B")
    assert b["fence_generation"] == gen_a + 1

    # Even an in-range late write from the old lease only sees conflict and
    # can never alter the bench under the new generation.
    r = await client.post(
        f"/benches/{bench_name}/setpoint",
        json={**a, "setpoint": 999},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "FENCE_MISMATCH"

    await client.post(
        f"/benches/{bench_name}/setpoint", json={**b, "setpoint": 42}
    )
    final = (await client.get(f"/benches/{bench_name}")).json()
    assert final["current_setpoint"] == 42
    assert final["lease"]["fence_generation"] == gen_a + 1
    assert final["lease"]["holder"] == "engineer-B"


@pytest.mark.asyncio
async def test_renew_deadline_recomputed_from_database_time(
    client, pool, bench_name
):
    await register(client, bench_name)
    held = await acquire(client, bench_name, "engineer-A", duration=5)
    a = creds(held, "engineer-A")

    # Walk the clock almost to the original deadline. The renew must add a
    # fresh full interval measured from current database time.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE benches SET expires_at = now() + interval '1 second' "
            "WHERE name = $1",
            bench_name,
        )

    r = await client.post(
        f"/benches/{bench_name}/renew",
        json={**a, "duration_seconds": 60},
    )
    assert r.status_code == 200
    new_deadline = datetime.fromisoformat(r.json()["lease"]["expires_at"])

    async with pool.acquire() as conn:
        db_now = await conn.fetchval("SELECT now()")
    remaining = (new_deadline - db_now).total_seconds()
    assert 55 <= remaining <= 61


@pytest.mark.asyncio
async def test_real_time_minimal_lease_expires(client, bench_name):
    """End-to-end check without touching the DB clock: 5 s is the minimum."""
    await register(client, bench_name, lo=0, hi=1000, initial=0)
    held = await acquire(client, bench_name, "engineer-A", duration=5)
    a = creds(held, "engineer-A")

    r = await client.post(
        f"/benches/{bench_name}/setpoint", json={**a, "setpoint": 1}
    )
    assert r.status_code == 200

    await asyncio.sleep(6.0)

    r = await client.post(
        f"/benches/{bench_name}/release", json=a
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "LEASE_EXPIRED"

    held_b = await acquire(client, bench_name, "engineer-B", duration=30)
    assert held_b["lease"]["fence_generation"] == a["fence_generation"] + 1
