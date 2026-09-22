"""Pydantic models for request bodies and JSON responses."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

# Allowed setpoint bounds are themselves bounded so everything fits in INTEGER.
ABSOLUTE_MIN = -1_000_000_000
ABSOLUTE_MAX = 1_000_000_000


def _clean(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be empty")
    return value


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    min_setpoint: int = Field(ge=ABSOLUTE_MIN, le=ABSOLUTE_MAX)
    max_setpoint: int = Field(ge=ABSOLUTE_MIN, le=ABSOLUTE_MAX)
    initial_setpoint: int = Field(ge=ABSOLUTE_MIN, le=ABSOLUTE_MAX)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean(v)

    @model_validator(mode="after")
    def _check_range(self) -> "RegisterRequest":
        if self.min_setpoint > self.max_setpoint:
            raise ValueError("min_setpoint must be <= max_setpoint")
        if not (self.min_setpoint <= self.initial_setpoint <= self.max_setpoint):
            raise ValueError(
                "initial_setpoint must be within [min_setpoint, max_setpoint]"
            )
        return self


class AcquireRequest(BaseModel):
    holder: str = Field(min_length=1, max_length=128)
    duration_seconds: int = Field(ge=5, le=300)

    @field_validator("holder")
    @classmethod
    def _holder(cls, v: str) -> str:
        return _clean(v)


class RenewRequest(BaseModel):
    holder: str = Field(min_length=1, max_length=128)
    lease_token: UUID
    fence_generation: int = Field(ge=1)
    duration_seconds: int = Field(ge=5, le=300)

    @field_validator("holder")
    @classmethod
    def _holder(cls, v: str) -> str:
        return _clean(v)


class ReleaseRequest(BaseModel):
    holder: str = Field(min_length=1, max_length=128)
    lease_token: UUID
    fence_generation: int = Field(ge=1)

    @field_validator("holder")
    @classmethod
    def _holder(cls, v: str) -> str:
        return _clean(v)


class SetpointRequest(BaseModel):
    holder: str = Field(min_length=1, max_length=128)
    lease_token: UUID
    fence_generation: int = Field(ge=1)
    setpoint: int = Field(ge=ABSOLUTE_MIN, le=ABSOLUTE_MAX)

    @field_validator("holder")
    @classmethod
    def _holder(cls, v: str) -> str:
        return _clean(v)


class LeaseInfo(BaseModel):
    lease_token: UUID
    holder: str
    fence_generation: int
    expires_at: datetime


class StatusResponse(BaseModel):
    name: str
    min_setpoint: int
    max_setpoint: int
    current_setpoint: int
    leased: bool
    lease: LeaseInfo | None = None


class ReleaseResponse(BaseModel):
    name: str
    released: bool
