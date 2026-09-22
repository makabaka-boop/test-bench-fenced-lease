"""统一的业务错误：所有可预期失败都以明确的 code + message 返回给调用方。"""

from __future__ import annotations


class AppError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def bench_not_found(name: str) -> AppError:
    return AppError(404, "BENCH_NOT_FOUND", f"bench '{name}' is not registered")


def lease_conflict(message: str) -> AppError:
    return AppError(409, "LEASE_CONFLICT", message)
