"""Registration validation and illegal operations leave no partial state."""
import pytest

from conftest import acquire, register


@pytest.mark.asyncio
async def test_register_and_get_status(client, bench_name):
    await register(client, bench_name, lo=-10, hi=10, initial=-3)
    r = await client.get(f"/benches/{bench_name}")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "name": bench_name,
        "min_setpoint": -10,
        "max_setpoint": 10,
        "current_setpoint": -3,
        "leased": False,
        "lease": None,
    }


@pytest.mark.asyncio
async def test_duplicate_name_conflict(client, bench_name):
    await register(client, bench_name)
    r = await client.post(
        "/benches",
        json={
            "name": bench_name,
            "min_setpoint": 0,
            "max_setpoint": 1,
            "initial_setpoint": 0,
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BENCH_ALREADY_EXISTS"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"name": " ", "min_setpoint": 0, "max_setpoint": 10,
         "initial_setpoint": 0},
        {"name": "bad-range", "min_setpoint": 10, "max_setpoint": 0,
         "initial_setpoint": 5},
        {"name": "bad-initial", "min_setpoint": 0, "max_setpoint": 10,
         "initial_setpoint": 11},
        {"name": "missing-initial", "min_setpoint": 0, "max_setpoint": 10},
    ],
)
async def test_invalid_registration_rejected(client, payload):
    r = await client.post("/benches", json=payload)
    assert r.status_code == 422, r.text
    name = (payload.get("name") or "").strip()
    if name:
        r = await client.get(f"/benches/{name}")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_acquire_duration_bounds(client, bench_name):
    await register(client, bench_name)
    for bad in (0, 4, 301, -5):
        r = await client.post(
            f"/benches/{bench_name}/lease",
            json={"holder": "engineer-A", "duration_seconds": bad},
        )
        assert r.status_code == 422, bad


@pytest.mark.asyncio
async def test_operations_on_unknown_bench_404(client):
    r = await client.get("/benches/does-not-exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "BENCH_NOT_FOUND"

    r = await client.post(
        "/benches/does-not-exist/lease",
        json={"holder": "x", "duration_seconds": 10},
    )
    assert r.status_code == 404

    r = await client.post(
        "/benches/does-not-exist/release",
        json={
            "holder": "x",
            "lease_token": "11111111-1111-1111-1111-111111111111",
            "fence_generation": 1,
        },
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_malformed_uuid_and_body(client, bench_name):
    await register(client, bench_name)
    await acquire(client, bench_name, "engineer-A")
    r = await client.post(
        f"/benches/{bench_name}/release",
        json={"holder": "engineer-A", "lease_token": "nope",
              "fence_generation": 1},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_failed_write_keeps_previous_state(client, bench_name):
    await register(client, bench_name, lo=0, hi=10, initial=7)
    held = await acquire(client, bench_name, "engineer-A")
    from conftest import creds
    a = creds(held, "engineer-A")
    r = await client.post(
        f"/benches/{bench_name}/setpoint", json={**a, "setpoint": 99}
    )
    assert r.status_code == 422
    state = (await client.get(f"/benches/{bench_name}")).json()
    assert state["current_setpoint"] == 7


@pytest.mark.asyncio
async def test_database_constraints_block_partial_state(client, pool, bench_name):
    """Defence in depth: even a direct out-of-range write is rejected by the
    database CHECK, so no row can ever keep a setpoint outside its range."""
    await register(client, bench_name, lo=0, hi=10, initial=0)
    with pytest.raises(Exception):
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE benches SET current_setpoint = 999 "
                    "WHERE name = $1",
                    bench_name,
                )

    # A half-set lease (token without expiry) is rejected as well.
    with pytest.raises(Exception):
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE benches SET lease_token = gen_random_uuid(), "
                    "holder = NULL, expires_at = NULL WHERE name = $1",
                    bench_name,
                )

    state = (await client.get(f"/benches/{bench_name}")).json()
    assert state["current_setpoint"] == 0
    assert state["leased"] is False
    assert state["lease"] is None
