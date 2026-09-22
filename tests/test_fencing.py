"""Interleaved release / setpoint traffic and the late-write fencing case."""
import pytest

from conftest import acquire, creds, register


@pytest.mark.asyncio
async def test_release_then_takeover_old_writes_are_rejected(
    client, bench_name
):
    await register(client, bench_name, lo=0, hi=1000, initial=100)
    held_a = await acquire(client, bench_name, "engineer-A")
    a = creds(held_a, "engineer-A")
    gen_a = a["fence_generation"]

    # Old holder writes legally while it still owns the bench.
    r = await client.post(
        f"/benches/{bench_name}/setpoint",
        json={**a, "setpoint": 300},
    )
    assert r.status_code == 200
    assert r.json()["current_setpoint"] == 300

    # Control is released and handed to a second engineer (new generation).
    r = await client.post(f"/benches/{bench_name}/release", json=a)
    assert r.status_code == 200
    assert r.json() == {"name": bench_name, "released": True}

    held_b = await acquire(client, bench_name, "engineer-B")
    b = creds(held_b, "engineer-B")
    assert b["fence_generation"] == gen_a + 1

    # New holder sets a new legal value.
    r = await client.post(
        f"/benches/{bench_name}/setpoint",
        json={**b, "setpoint": 720},
    )
    assert r.status_code == 200

    # A late write from the old holder/old generation must fail, and the
    # bench must keep the new holder's last legal setpoint.
    late = await client.post(
        f"/benches/{bench_name}/setpoint",
        json={**a, "setpoint": 999},
    )
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "FENCE_MISMATCH"

    late_release = await client.post(
        f"/benches/{bench_name}/release", json=a
    )
    assert late_release.status_code == 409
    assert late_release.json()["error"]["code"] == "FENCE_MISMATCH"

    state = await client.get(f"/benches/{bench_name}")
    body = state.json()
    assert body["current_setpoint"] == 720
    assert body["lease"]["fence_generation"] == gen_a + 1
    assert body["lease"]["holder"] == "engineer-B"


@pytest.mark.asyncio
async def test_setpoint_range_validation(client, bench_name):
    await register(client, bench_name, lo=-50, hi=50, initial=0)
    held = await acquire(client, bench_name, "engineer-A")
    a = creds(held, "engineer-A")

    for bad in (-51, 51, 1000):
        r = await client.post(
            f"/benches/{bench_name}/setpoint",
            json={**a, "setpoint": bad},
        )
        assert r.status_code == 422, (bad, r.text)
        assert r.json()["error"]["code"] == "SETPOINT_OUT_OF_RANGE"

    for good in (-50, 0, 50):
        r = await client.post(
            f"/benches/{bench_name}/setpoint",
            json={**a, "setpoint": good},
        )
        assert r.status_code == 200, (good, r.text)
        assert r.json()["current_setpoint"] == good


@pytest.mark.asyncio
async def test_renew_rejects_wrong_generation_and_extends(client, bench_name):
    await register(client, bench_name)
    held = await acquire(client, bench_name, "engineer-A", duration=10)
    a = creds(held, "engineer-A")

    forged = {**a, "fence_generation": a["fence_generation"] + 999}
    r = await client.post(
        f"/benches/{bench_name}/renew",
        json={**forged, "duration_seconds": 60},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "FENCE_MISMATCH"

    r = await client.post(
        f"/benches/{bench_name}/renew",
        json={**a, "duration_seconds": 300},
    )
    assert r.status_code == 200
    assert r.json()["lease"]["fence_generation"] == a["fence_generation"]


@pytest.mark.asyncio
async def test_wrong_token_is_fence_mismatch(client, bench_name):
    await register(client, bench_name)
    held = await acquire(client, bench_name, "engineer-A")
    a = creds(held, "engineer-A")

    r = await client.post(
        f"/benches/{bench_name}/setpoint",
        json={
            "holder": "engineer-A",
            "lease_token": "00000000-0000-0000-0000-000000000000",
            "fence_generation": a["fence_generation"],
            "setpoint": 10,
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "FENCE_MISMATCH"
