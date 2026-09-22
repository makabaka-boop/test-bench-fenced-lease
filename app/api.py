"""HTTP 接口层。

一致性设计：
- 每个变更都是一条带条件的 UPDATE，由数据库行锁串行化并发竞争；
- 到期判断只用数据库时间 now()，续租也从 now() 重新计算；
- 续租、释放、写入必须同时匹配租约令牌与栅栏代次，且租约未到期；
- 任何失败都整体回滚，不留下部分状态。
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends
from sqlalchemy import extract, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import get_session
from .errors import AppError, bench_not_found, lease_conflict
from .models import Bench
from .schemas import (
    AcquireIn,
    BenchRegisterIn,
    BenchStatusOut,
    LeaseOut,
    LeaseStatusOut,
    ReleaseIn,
    ReleaseOut,
    RenewIn,
    SetpointIn,
    SetpointOut,
)

router = APIRouter()

_LEASE_ACTIVE = Bench.lease_expires_at > func.now()
_REMAINING = extract("epoch", Bench.lease_expires_at - func.now())


def _lease_interval(seconds: int):
    """now() + interval 全部在数据库内计算，绝不使用应用服务器时钟。"""
    return func.make_interval(0, 0, 0, 0, 0, 0, seconds)


def _status_row(session: Session, name: str):
    return session.execute(
        select(
            Bench,
            _LEASE_ACTIVE.label("active"),
            _REMAINING.label("remaining"),
        ).where(Bench.name == name)
    ).first()


def _to_status(
    bench: Bench, active: bool | None, remaining: float | None
) -> BenchStatusOut:
    lease = None
    if bench.lease_token is not None:
        lease = LeaseStatusOut(
            holder=bench.lease_holder or "",
            expires_at=bench.lease_expires_at,
            active=bool(active),
            remaining_seconds=max(0.0, float(remaining or 0.0)),
        )
    return BenchStatusOut(
        name=bench.name,
        min_setpoint=bench.min_setpoint,
        max_setpoint=bench.max_setpoint,
        current_setpoint=bench.current_setpoint,
        fence_generation=bench.fence_generation,
        lease=lease,
    )


def _classify_lease_failure(
    session: Session, name: str, token: str, generation: int
) -> AppError:
    """条件更新命中 0 行后读取当前行，给出明确的冲突原因（仅用于错误信息，
    并发正确性由 UPDATE 本身的条件保证）。"""
    row = _status_row(session, name)
    if row is None:
        return bench_not_found(name)
    bench, active, _ = row
    if bench.lease_token is None:
        return lease_conflict("no active lease: the lease was released")
    if not active:
        return lease_conflict("lease is expired; acquire a new lease")
    if bench.lease_token != token:
        return lease_conflict("lease token mismatch: bench is held by another lease")
    if bench.fence_generation != generation:
        return lease_conflict(
            "fencing generation mismatch: "
            f"current generation is {bench.fence_generation}"
        )
    return lease_conflict("lease conflict")


@router.post("/benches", response_model=BenchStatusOut, status_code=201)
def register_bench(
    payload: BenchRegisterIn, session: Session = Depends(get_session)
):
    bench = Bench(
        name=payload.name,
        min_setpoint=payload.min_setpoint,
        max_setpoint=payload.max_setpoint,
        fence_generation=0,
    )
    session.add(bench)
    try:
        session.commit()
    except IntegrityError:
        # 唯一约束兜底：并发登记同名试验台时只有一个成功
        session.rollback()
        raise AppError(
            409, "BENCH_EXISTS", f"bench '{payload.name}' is already registered"
        )
    return _to_status(bench, None, None)


@router.get("/benches", response_model=list[BenchStatusOut])
def list_benches(session: Session = Depends(get_session)):
    rows = session.execute(
        select(
            Bench,
            _LEASE_ACTIVE.label("active"),
            _REMAINING.label("remaining"),
        ).order_by(Bench.name)
    ).all()
    return [_to_status(bench, active, remaining) for bench, active, remaining in rows]


@router.get("/benches/{name}", response_model=BenchStatusOut)
def get_bench(name: str, session: Session = Depends(get_session)):
    row = _status_row(session, name)
    if row is None:
        raise bench_not_found(name)
    return _to_status(*row)


@router.post("/benches/{name}/lease/acquire", response_model=LeaseOut)
def acquire_lease(
    name: str, payload: AcquireIn, session: Session = Depends(get_session)
):
    token = secrets.token_hex(16)
    stmt = (
        update(Bench)
        .where(Bench.name == name)
        .where(
            or_(
                Bench.lease_token.is_(None),
                Bench.lease_expires_at <= func.now(),
            )
        )
        .values(
            lease_token=token,
            lease_holder=payload.holder,
            fence_generation=Bench.fence_generation + 1,
            lease_expires_at=func.now() + _lease_interval(payload.duration_seconds),
        )
        .returning(Bench.fence_generation, Bench.lease_expires_at)
    )
    row = session.execute(stmt).first()
    if row is None:
        session.rollback()
        if _status_row(session, name) is None:
            raise bench_not_found(name)
        raise AppError(
            409,
            "LEASE_HELD",
            "bench already has an active lease held by another holder",
        )
    generation, expires_at = row
    session.commit()
    return LeaseOut(
        name=name,
        token=token,
        holder=payload.holder,
        generation=generation,
        expires_at=expires_at,
    )


@router.post("/benches/{name}/lease/renew", response_model=LeaseOut)
def renew_lease(
    name: str, payload: RenewIn, session: Session = Depends(get_session)
):
    stmt = (
        update(Bench)
        .where(
            Bench.name == name,
            Bench.lease_token == payload.token,
            Bench.fence_generation == payload.generation,
            _LEASE_ACTIVE,
        )
        .values(lease_expires_at=func.now() + _lease_interval(payload.duration_seconds))
        .returning(Bench.lease_expires_at, Bench.lease_holder)
    )
    row = session.execute(stmt).first()
    if row is None:
        session.rollback()
        raise _classify_lease_failure(
            session, name, payload.token, payload.generation
        )
    expires_at, holder = row
    session.commit()
    return LeaseOut(
        name=name,
        token=payload.token,
        holder=holder,
        generation=payload.generation,
        expires_at=expires_at,
    )


@router.post("/benches/{name}/lease/release", response_model=ReleaseOut)
def release_lease(
    name: str, payload: ReleaseIn, session: Session = Depends(get_session)
):
    stmt = (
        update(Bench)
        .where(
            Bench.name == name,
            Bench.lease_token == payload.token,
            Bench.fence_generation == payload.generation,
            _LEASE_ACTIVE,
        )
        .values(lease_token=None, lease_holder=None, lease_expires_at=None)
        .returning(Bench.name)
    )
    if session.execute(stmt).first() is None:
        session.rollback()
        raise _classify_lease_failure(
            session, name, payload.token, payload.generation
        )
    session.commit()
    return ReleaseOut(name=name, released=True)


@router.put("/benches/{name}/setpoint", response_model=SetpointOut)
def write_setpoint(
    name: str, payload: SetpointIn, session: Session = Depends(get_session)
):
    stmt = (
        update(Bench)
        .where(
            Bench.name == name,
            Bench.lease_token == payload.token,
            Bench.fence_generation == payload.generation,
            _LEASE_ACTIVE,
            Bench.min_setpoint <= payload.value,
            Bench.max_setpoint >= payload.value,
        )
        .values(current_setpoint=payload.value)
        .returning(Bench.current_setpoint)
    )
    if session.execute(stmt).first() is None:
        session.rollback()
        row = _status_row(session, name)
        if row is None:
            raise bench_not_found(name)
        bench, active, _ = row
        lease_ok = (
            bench.lease_token is not None
            and bool(active)
            and bench.lease_token == payload.token
            and bench.fence_generation == payload.generation
        )
        if not lease_ok:
            # 旧持有者的迟到写入只能看到明确冲突
            raise _classify_lease_failure(
                session, name, payload.token, payload.generation
            )
        raise AppError(
            400,
            "SETPOINT_OUT_OF_RANGE",
            f"value {payload.value} is outside the registered range "
            f"[{bench.min_setpoint}, {bench.max_setpoint}]",
        )
    session.commit()
    return SetpointOut(
        name=name, current_setpoint=payload.value, generation=payload.generation
    )


@router.get("/health")
def health(session: Session = Depends(get_session)):
    session.execute(text("SELECT 1"))
    return {"status": "ok"}
