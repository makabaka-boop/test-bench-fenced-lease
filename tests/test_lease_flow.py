"""端到端测试：真实 PostgreSQL 上的并发取得、释放/写入交错、到期接管与重启恢复。"""

from __future__ import annotations

import threading
import time
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import create_app

BENCH = {"name": "bench-1", "min_setpoint": 0, "max_setpoint": 100}


def _register(client, **override):
    return client.post("/benches", json={**BENCH, **override})


def _acquire(client, holder="alice", duration=30, name=BENCH["name"]):
    return client.post(
        f"/benches/{name}/lease/acquire",
        json={"holder": holder, "duration_seconds": duration},
    )


def _renew(client, token, generation, duration=60, name=BENCH["name"]):
    return client.post(
        f"/benches/{name}/lease/renew",
        json={
            "token": token,
            "generation": generation,
            "duration_seconds": duration,
        },
    )


def _release(client, token, generation, name=BENCH["name"]):
    return client.post(
        f"/benches/{name}/lease/release",
        json={"token": token, "generation": generation},
    )


def _write(client, token, generation, value, name=BENCH["name"]):
    return client.put(
        f"/benches/{name}/setpoint",
        json={"token": token, "generation": generation, "value": value},
    )


def _status(client, name=BENCH["name"]):
    resp = client.get(f"/benches/{name}")
    assert resp.status_code == 200
    return resp.json()


def _wait_for_expiry(client, name=BENCH["name"], timeout=15.0):
    """轮询状态接口（到期与否由数据库时间判定），直到租约不再有效。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lease = _status(client, name)["lease"]
        if lease is None or not lease["active"]:
            return
        time.sleep(0.2)
    raise TimeoutError("lease did not expire in time")


def test_register_validation_and_no_partial_state(client):
    resp = _register(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["fence_generation"] == 0
    assert body["current_setpoint"] is None
    assert body["lease"] is None

    # 重名 → 409，原记录不受影响
    dup = _register(client)
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "BENCH_EXISTS"
    assert _status(client)["min_setpoint"] == 0

    # 非法范围 → 422，且不得留下任何记录
    bad = _register(client, name="bench-bad", min_setpoint=50, max_setpoint=10)
    assert bad.status_code == 422
    assert client.get("/benches/bench-bad").status_code == 404

    # 非法名称 → 422
    assert _register(client, name="").status_code == 422
    assert _register(client, name="bad name!").status_code == 422

    # 对未登记试验台的操作 → 404，不产生任何状态
    assert _acquire(client, name="ghost").status_code == 404
    assert _renew(client, "t", 1, name="ghost").status_code == 404
    assert _release(client, "t", 1, name="ghost").status_code == 404
    assert _write(client, "t", 1, 10, name="ghost").status_code == 404
    assert [b["name"] for b in client.get("/benches").json()] == ["bench-1"]


def test_acquire_duration_validation(client):
    _register(client)
    assert _acquire(client, duration=4).status_code == 422
    assert _acquire(client, duration=301).status_code == 422
    assert _acquire(client, holder="").status_code == 422
    # 失败的取得不得改变任何状态
    status = _status(client)
    assert status["fence_generation"] == 0
    assert status["lease"] is None


def test_concurrent_acquire_has_exactly_one_winner(client):
    _register(client)
    workers = 8
    barrier = threading.Barrier(workers)
    results = []
    lock = threading.Lock()

    def worker(i):
        # 每个线程使用独立的应用实例与连接，模拟两个客户端同时远程操作
        with TestClient(create_app()) as c:
            barrier.wait(timeout=10)
            resp = _acquire(c, holder=f"engineer-{i}", duration=60)
            with lock:
                results.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for t in threads:
        assert not t.is_alive()

    wins = [r for r in results if r[0] == 200]
    conflicts = [r for r in results if r[0] == 409]
    assert len(wins) == 1
    assert len(conflicts) == workers - 1
    for _, body in conflicts:
        assert body["error"]["code"] == "LEASE_HELD"

    winner = wins[0][1]
    assert winner["generation"] == 1
    status = _status(client)
    assert status["fence_generation"] == 1
    assert status["lease"]["holder"] == winner["holder"]
    assert status["lease"]["active"] is True


def test_write_requires_valid_lease_and_stays_in_range(client):
    _register(client)
    # 无租约直接写 → 409
    resp = _write(client, token="nope", generation=1, value=10)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "LEASE_CONFLICT"

    lease = _acquire(client).json()
    token, gen = lease["token"], lease["generation"]

    # 越界 → 400，且不改变当前值
    for bad_value in (-1, 101):
        resp = _write(client, token, gen, bad_value)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "SETPOINT_OUT_OF_RANGE"
    assert _status(client)["current_setpoint"] is None

    # 合法写入
    assert _write(client, token, gen, 55).status_code == 200
    assert _status(client)["current_setpoint"] == 55

    # 令牌/代次不匹配 → 409，当前值不变
    assert _write(client, "wrong-token", gen, 60).status_code == 409
    assert _write(client, token, gen + 1, 60).status_code == 409
    assert _status(client)["current_setpoint"] == 55


def test_release_and_write_interleaving(client):
    _register(client)
    lease_a = _acquire(client, holder="alice").json()
    token_a, gen_a = lease_a["token"], lease_a["generation"]
    assert _write(client, token_a, gen_a, 10).status_code == 200

    # 释放后旧令牌立即失效；重复释放同样失败
    assert _release(client, token_a, gen_a).status_code == 200
    assert _write(client, token_a, gen_a, 11).status_code == 409
    assert _release(client, token_a, gen_a).status_code == 409
    assert _renew(client, token_a, gen_a).status_code == 409

    # 新持有者取得 → 代次递增
    lease_b = _acquire(client, holder="bob").json()
    token_b, gen_b = lease_b["token"], lease_b["generation"]
    assert gen_b == gen_a + 1
    assert _write(client, token_b, gen_b, 20).status_code == 200

    # 有效租约期间任何人（含原持有者、现持有者自己）都无法再次取得
    assert _acquire(client, holder="alice").status_code == 409
    assert _acquire(client, holder="bob").status_code == 409

    # 旧持有者的迟到写入 → 明确冲突，设定值保持
    stale = _write(client, token_a, gen_a, 99)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "LEASE_CONFLICT"
    status = _status(client)
    assert status["current_setpoint"] == 20
    assert status["fence_generation"] == gen_b
    assert status["lease"]["holder"] == "bob"


def test_renew_extends_lease_and_requires_matching_fence(client):
    _register(client)
    lease = _acquire(client, duration=30).json()
    token, gen = lease["token"], lease["generation"]

    # 令牌/代次不匹配 → 409；租期非法 → 422
    assert _renew(client, "wrong", gen).status_code == 409
    assert _renew(client, token, gen + 1).status_code == 409
    assert _renew(client, token, gen, duration=1000).status_code == 422

    renewed = _renew(client, token, gen, duration=120)
    assert renewed.status_code == 200
    new_expiry = datetime.fromisoformat(renewed.json()["expires_at"])
    old_expiry = datetime.fromisoformat(lease["expires_at"])
    assert new_expiry > old_expiry
    # 续租从数据库当前时间重新计算：剩余时间应接近新租期而非旧租期的累加
    remaining = _status(client)["lease"]["remaining_seconds"]
    assert 100 < remaining <= 120
    # 续租不改变代次
    assert _status(client)["fence_generation"] == gen


def test_takeover_after_expiry_and_stale_writer_gets_conflict(client):
    _register(client)
    lease_a = _acquire(client, holder="alice", duration=5).json()
    token_a, gen_a = lease_a["token"], lease_a["generation"]
    assert _write(client, token_a, gen_a, 11).status_code == 200

    _wait_for_expiry(client)

    # 到期后旧持有者的续租/写入全部失败
    assert _renew(client, token_a, gen_a).status_code == 409
    assert _write(client, token_a, gen_a, 12).status_code == 409

    # 竞争者接管，代次递增
    lease_b = _acquire(client, holder="bob", duration=60).json()
    token_b, gen_b = lease_b["token"], lease_b["generation"]
    assert gen_b == gen_a + 1
    assert _write(client, token_b, gen_b, 77).status_code == 200

    # 旧请求最后到达：只能看到明确冲突（即使值本身越界也先报租约冲突），
    # 试验台保持新代次下最后一次合法设定
    stale = _write(client, token_a, gen_a, 999)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "LEASE_CONFLICT"
    assert _release(client, token_a, gen_a).status_code == 409

    status = _status(client)
    assert status["current_setpoint"] == 77
    assert status["fence_generation"] == gen_b
    assert status["lease"]["holder"] == "bob"
    assert status["lease"]["active"] is True


def test_state_survives_application_restart(client):
    _register(client)
    lease = _acquire(client, holder="alice", duration=120).json()
    token, gen = lease["token"], lease["generation"]
    assert _write(client, token, gen, 42).status_code == 200

    # 模拟服务重启：丢弃全部数据库连接、重建应用实例，数据库保持不变
    from app.db import engine

    engine.dispose()
    with TestClient(create_app()) as restarted:
        status = _status(restarted)
        assert status["current_setpoint"] == 42
        assert status["fence_generation"] == gen
        assert status["lease"]["holder"] == "alice"
        assert status["lease"]["active"] is True

        # 重启前签发的令牌与代次仍然有效；错误的代次仍然被拒绝
        assert _write(restarted, token, gen, 43).status_code == 200
        assert _write(restarted, token, gen + 1, 44).status_code == 409
        assert _status(restarted)["current_setpoint"] == 43
