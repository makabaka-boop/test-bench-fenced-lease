"""PostgreSQL schema for the high-voltage test bench service.

All lease state lives in a single row per bench. Invariants are enforced by
CHECK constraints so that an aborted/illegal operation can never leave a row
in a half-updated state (no token without expiry, setpoint out of range, ...).
"""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS benches (
    name              TEXT PRIMARY KEY,
    min_setpoint      INTEGER NOT NULL,
    max_setpoint      INTEGER NOT NULL,
    current_setpoint  INTEGER NOT NULL,
    -- Fencing generation per bench: starts at 0 (never leased) and is
    -- incremented atomically on every successful acquire. A rolled-back
    -- acquire does not advance it.
    fence_generation  BIGINT  NOT NULL DEFAULT 0,
    -- Token issued to the current holder; NULL while the bench is free.
    lease_token       UUID,
    holder            TEXT,
    expires_at        TIMESTAMPTZ,

    CONSTRAINT benches_range_orientation
        CHECK (min_setpoint <= max_setpoint),
    CONSTRAINT benches_setpoint_in_range
        CHECK (current_setpoint BETWEEN min_setpoint AND max_setpoint),
    -- Lease columns are all present or all absent (no partial lease state).
    CONSTRAINT benches_lease_all_or_nothing
        CHECK (
            (lease_token IS NULL AND holder IS NULL AND expires_at IS NULL)
            OR
            (lease_token IS NOT NULL AND holder IS NOT NULL
             AND expires_at IS NOT NULL)
        )
);
"""


async def init_schema(conn) -> None:
    await conn.execute(SCHEMA_SQL)
