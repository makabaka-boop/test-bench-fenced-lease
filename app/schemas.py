"""请求/响应模型，全部使用普通 JSON。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

# 租约租期的合法范围（秒）
MIN_LEASE_SECONDS = 5
MAX_LEASE_SECONDS = 300


class BenchRegisterIn(BaseModel):
    name: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    min_setpoint: int
    max_setpoint: int

    @model_validator(mode="after")
    def _range_not_inverted(self) -> BenchRegisterIn:
        if self.min_setpoint > self.max_setpoint:
            raise ValueError("min_setpoint must be <= max_setpoint")
        return self


class AcquireIn(BaseModel):
    holder: str = Field(min_length=1, max_length=128)
    duration_seconds: int = Field(ge=MIN_LEASE_SECONDS, le=MAX_LEASE_SECONDS)


class RenewIn(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    generation: int = Field(ge=0)
    duration_seconds: int = Field(ge=MIN_LEASE_SECONDS, le=MAX_LEASE_SECONDS)


class ReleaseIn(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    generation: int = Field(ge=0)


class SetpointIn(BaseModel):
    token: str = Field(min_length=1, max_length=64)
    generation: int = Field(ge=0)
    value: int


class LeaseOut(BaseModel):
    name: str
    token: str
    holder: str
    generation: int
    expires_at: datetime


class LeaseStatusOut(BaseModel):
    holder: str
    expires_at: datetime
    active: bool
    remaining_seconds: float


class BenchStatusOut(BaseModel):
    name: str
    min_setpoint: int
    max_setpoint: int
    current_setpoint: int | None
    fence_generation: int
    lease: LeaseStatusOut | None


class SetpointOut(BaseModel):
    name: str
    current_setpoint: int
    generation: int


class ReleaseOut(BaseModel):
    name: str
    released: bool
