from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base


class SplitPlan(Base):
    """卷料防串样的训练/独立验证划分快照，版本号随卷料登记单调递增。"""

    __tablename__ = 'split_plans'

    id: Mapped[int] = mapped_column(primary_key=True)
    coil_version: Mapped[int] = mapped_column(unique=True, index=True)
    plan: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String(32))
