"""Initial versioned Dugout Lab schema.

Revision ID: 20260822_01
Revises:
"""
from __future__ import annotations

from alembic import op

from backend.app.database.base import Base
from backend.app.models import entities  # noqa: F401


revision = "20260822_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Metadata is the canonical schema. create_all is idempotent for existing small local installations.
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    # Data-preserving policy: destructive whole-schema downgrade is intentionally not automatic.
    raise RuntimeError("자동 전체 삭제는 지원하지 않습니다. 백업 복구 또는 명시적 테이블 마이그레이션을 사용하세요.")
