"""运行时配置，仅通过环境变量注入。"""

from __future__ import annotations

import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://hvtest:hvtest@127.0.0.1:5432/hvtest",
)
