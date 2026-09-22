"""试验台持久化模型：登记信息、当前设定值、租约与栅栏代次。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Bench(Base):
    __tablename__ = "benches"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 唯一试验台名
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 允许的整数设定范围（闭区间）
    min_setpoint: Mapped[int] = mapped_column(Integer, nullable=False)
    max_setpoint: Mapped[int] = mapped_column(Integer, nullable=False)
    # 当前设定值，首次合法写入前为 NULL
    current_setpoint: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 租约标识（服务端生成的随机令牌）与持有者
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 栅栏代次：每次取得租约单调递增，永不回退
    fence_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    # 租约到期时间，一律由数据库 now() 计算
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
