"""FastAPI application: test bench registry with fenced leases.

Fencing model
--------------
Each successful ``acquire`` bumps the bench's ``fence_generation`` counter
(starting at 1 for the first lease) and issues a fresh random ``lease_token``. Renew / release / setpoint writes
must present *both* values and are executed as single row-level atomic
UPDATEs; the liveness predicate uses database time only (``now()``), so a
request arriving late — after the lease expired and a new holder took over —
is rejected even if the old client's clock disagrees. The previous holder
can never mutate a bench it no longer owns, and cannot roll the generation
back: the bench keeps the new generation and the last legal setpoint.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .db import init_schema
from .schemas import (
    AcquireRequest,
    RegisterRequest,
    ReleaseRequest,
    ReleaseResponse,
    RenewRequest,
    SetpointRequest,
    StatusResponse,
)

# Row locked FOR UPDATE; liveness is computed with database time exclusively.
LOCK_SELECT = """
    SELECT name, min_setpoint, max_setpoint, current_setpoint,
           fence_generation, lease_token, holder, expires_at,
           (expires_at IS NOT NULL AND now() < expires_at) AS active
      FROM benches
     WHERE name = $1
     FOR UPDATE
"""

STATUS_SELECT = """
    SELECT name, min_setpoint, max_setpoint, current_setpoint,
           fence_generation, lease_token, holder, expires_at,
           (expires_at IS NOT NULL AND now() < expires_at) AS active
      FROM benches
     WHERE name = $1
"""


class ErrorJSONResponse(JSONResponse):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(
            status_code=status_code,
            content={"error": {"code": code, "message": message}},
        )


def _lease_status(row) -> StatusResponse:
    lease = None
    leased = bool(row["active"])
    # Token is only exposed while the lease is alive; a stale row whose expiry
    # has passed is reported as free (and only DB time decides that).
    if leased:
        lease = {
            "lease_token": str(row["lease_token"]),
            "holder": row["holder"],
            "fence_generation": row["fence_generation"],
            "expires_at": row["expires_at"].isoformat(),
        }
    return StatusResponse(
        name=row["name"],
        min_setpoint=row["min_setpoint"],
        max_setpoint=row["max_setpoint"],
        current_setpoint=row["current_setpoint"],
        leased=leased,
        lease=lease,
    )


def create_app(database_url: str | None = None) -> FastAPI:
    database_url = database_url or os.environ["DATABASE_URL"]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = await asyncpg.create_pool(
            database_url, min_size=1, max_size=10, timeout=30
        )
        async with pool.acquire() as conn:
            await init_schema(conn)
        app.state.pool = pool
        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(
        title="High-Voltage Test Bench Service",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return ErrorJSONResponse(
                exc.status_code, detail["code"], detail["message"]
            )
        return ErrorJSONResponse(exc.status_code, "HTTP_ERROR", str(detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request, exc: RequestValidationError
    ):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "request body failed validation",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        # Log internally; never leak internal exception text to clients.
        import logging

        logging.getLogger("hv-bench").exception("unhandled error: %r", exc)
        return ErrorJSONResponse(
            500, "INTERNAL_ERROR", "internal server error"
        )

    @app.get("/health")
    async def health() -> dict:
        async with app.state.pool.acquire() as conn:
            await conn.execute("SELECT 1")
        return {"status": "ok"}

    @app.post("/benches", status_code=201, response_model=StatusResponse)
    async def register(body: RegisterRequest) -> StatusResponse:
        async with app.state.pool.acquire() as conn:
            try:
                # INSERT is a single statement; on failure the transaction is
                # rolled back and no partial bench row is ever visible.
                row = await conn.fetchrow(
                    """
                    INSERT INTO benches (
                        name, min_setpoint, max_setpoint, current_setpoint,
                        lease_token, holder, expires_at
                    ) VALUES ($1, $2, $3, $4, NULL, NULL, NULL)
                    RETURNING name, min_setpoint, max_setpoint, current_setpoint,
                              fence_generation, lease_token, holder, expires_at,
                              FALSE AS active
                    """,
                    body.name,
                    body.min_setpoint,
                    body.max_setpoint,
                    body.initial_setpoint,
                )
            except asyncpg.UniqueViolationError:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "BENCH_ALREADY_EXISTS",
                        "message": f"bench {body.name!r} is already registered",
                    },
                )
            except asyncpg.CheckViolationError:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "INVALID_RANGE",
                        "message": "setpoint range constraints were violated",
                    },
                )
        return _lease_status(row)

    @app.post("/benches/{name}/lease", response_model=StatusResponse)
    async def acquire(name: str, body: AcquireRequest) -> StatusResponse:
        async with app.state.pool.acquire() as conn:
            async with conn.transaction():
                # Single atomic UPDATE ... WHERE active is false: concurrent
                # acquirers serialize on the row lock, exactly one wins.
                row = await conn.fetchrow(
                    """
                    UPDATE benches
                       SET fence_generation = fence_generation + 1,
                           lease_token      = gen_random_uuid(),
                           holder           = $2,
                           expires_at       = now()
                                              + make_interval(secs => $3)
                     WHERE name = $1
                       AND (expires_at IS NULL OR now() >= expires_at)
                    RETURNING name, min_setpoint, max_setpoint, current_setpoint,
                              fence_generation, lease_token, holder, expires_at,
                              TRUE AS active
                    """,
                    name,
                    body.holder,
                    body.duration_seconds,
                )
                if row is None:
                    existing = await conn.fetchrow(STATUS_SELECT, name)
                    if existing is None:
                        raise HTTPException(
                            status_code=404,
                            detail={
                                "code": "BENCH_NOT_FOUND",
                                "message": f"bench {name!r} is not registered",
                            },
                        )
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "LEASE_ACTIVE",
                            "message": f"bench {name!r} is already leased "
                            f"by {existing['holder']!r} until "
                            f"{existing['expires_at'].isoformat()}",
                        },
                    )
        return _lease_status(row)

    @app.post("/benches/{name}/renew", response_model=StatusResponse)
    async def renew(name: str, body: RenewRequest) -> StatusResponse:
        async with app.state.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(LOCK_SELECT, name)
                if row is None:
                    raise HTTPException(
                        status_code=404,
                        detail={
                            "code": "BENCH_NOT_FOUND",
                            "message": f"bench {name!r} is not registered",
                        },
                    )
                if not row["active"]:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "LEASE_EXPIRED",
                            "message": "no active lease for this fence "
                            "generation; acquire again",
                        },
                    )
                if (
                    row["lease_token"] != body.lease_token
                    or row["fence_generation"] != body.fence_generation
                    or row["holder"] != body.holder
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "FENCE_MISMATCH",
                            "message": "holder, lease token or fence "
                            "generation does not match the current lease",
                        },
                    )
                # New deadline is measured from current database time.
                row = await conn.fetchrow(
                    """
                    UPDATE benches
                       SET expires_at = now() + make_interval(secs => $2)
                     WHERE name = $1
                    RETURNING name, min_setpoint, max_setpoint, current_setpoint,
                              fence_generation, lease_token, holder, expires_at,
                              TRUE AS active
                    """,
                    name,
                    body.duration_seconds,
                )
        return _lease_status(row)

    @app.post("/benches/{name}/release", response_model=ReleaseResponse)
    async def release(name: str, body: ReleaseRequest) -> ReleaseResponse:
        async with app.state.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(LOCK_SELECT, name)
                if row is None:
                    raise HTTPException(
                        status_code=404,
                        detail={
                            "code": "BENCH_NOT_FOUND",
                            "message": f"bench {name!r} is not registered",
                        },
                    )
                if not row["active"]:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "LEASE_EXPIRED",
                            "message": "lease already expired or released",
                        },
                    )
                if (
                    row["lease_token"] != body.lease_token
                    or row["fence_generation"] != body.fence_generation
                    or row["holder"] != body.holder
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "FENCE_MISMATCH",
                            "message": "holder, lease token or fence "
                            "generation does not match the current lease",
                        },
                    )
                # Clearing any lease column clears them all via CHECK.
                await conn.execute(
                    """
                    UPDATE benches
                       SET lease_token = NULL, holder = NULL, expires_at = NULL
                     WHERE name = $1
                    """,
                    name,
                )
        return ReleaseResponse(name=name, released=True)

    @app.post("/benches/{name}/setpoint", response_model=StatusResponse)
    async def write_setpoint(name: str, body: SetpointRequest) -> StatusResponse:
        async with app.state.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(LOCK_SELECT, name)
                if row is None:
                    raise HTTPException(
                        status_code=404,
                        detail={
                            "code": "BENCH_NOT_FOUND",
                            "message": f"bench {name!r} is not registered",
                        },
                    )
                # Fence is checked first: a late write from an ousted holder
                # must see a conflict regardless of its payload.
                if not row["active"]:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "LEASE_EXPIRED",
                            "message": "no active lease for this fence "
                            "generation; acquire again",
                        },
                    )
                if (
                    row["lease_token"] != body.lease_token
                    or row["fence_generation"] != body.fence_generation
                    or row["holder"] != body.holder
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "FENCE_MISMATCH",
                            "message": "holder, lease token or fence "
                            "generation does not match the current lease",
                        },
                    )
                if not (
                    row["min_setpoint"] <= body.setpoint <= row["max_setpoint"]
                ):
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "SETPOINT_OUT_OF_RANGE",
                            "message": f"setpoint must be within "
                            f"[{row['min_setpoint']}, {row['max_setpoint']}]",
                        },
                    )
                row = await conn.fetchrow(
                    """
                    UPDATE benches
                       SET current_setpoint = $2
                     WHERE name = $1
                    RETURNING name, min_setpoint, max_setpoint, current_setpoint,
                              fence_generation, lease_token, holder, expires_at,
                              TRUE AS active
                    """,
                    name,
                    body.setpoint,
                )
        return _lease_status(row)

    @app.get("/benches/{name}", response_model=StatusResponse)
    async def status(name: str) -> StatusResponse:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow(STATUS_SELECT, name)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "BENCH_NOT_FOUND",
                    "message": f"bench {name!r} is not registered",
                },
            )
        return _lease_status(row)

    return app


app = create_app()
