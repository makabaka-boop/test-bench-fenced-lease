"""Two clients racing for one bench: exactly one must win."""
import asyncio

import pytest

from conftest import acquire, register


@pytest.mark.asyncio
async def test_concurrent_acquire_only_one_wins(client, bench_name):
    await register(client, bench_name)

    async def take(holder):
        return await client.post(
            f"/benches/{bench_name}/lease",
            json={"holder": holder, "duration_seconds": 30},
        )

    results = await asyncio.gather(
        take("engineer-A"), take("engineer-B")
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409]

    winner = next(r for r in results if r.status_code == 200).json()
    loser = next(r for r in results if r.status_code == 409).json()
    assert loser["error"]["code"] == "LEASE_ACTIVE"
    assert winner["lease"]["holder"] in {"engineer-A", "engineer-B"}

    # The loser still cannot take it while the lease is alive.
    again = await client.post(
        f"/benches/{bench_name}/lease",
        json={"holder": "engineer-B", "duration_seconds": 30},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "LEASE_ACTIVE"


@pytest.mark.asyncio
async def test_concurrent_acquire_after_expiry_single_winner(
    client, pool, bench_name
):
    await register(client, bench_name)
    held = await acquire(client, bench_name, "engineer-A", duration=10)
    gen_a = held["lease"]["fence_generation"]

    # Force the first lease into the past using the database clock.
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE benches SET expires_at = now() - interval '1 second' "
            "WHERE name = $1",
            bench_name,
        )

    async def take(holder):
        return await client.post(
            f"/benches/{bench_name}/lease",
            json={"holder": holder, "duration_seconds": 10},
        )

    results = await asyncio.gather(
        take("engineer-A"), take("engineer-B")
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409]
    winner = next(r for r in results if r.status_code == 200).json()
    assert winner["lease"]["fence_generation"] == gen_a + 1
