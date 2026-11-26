from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base


class CoilRegistration(Base):
    """卷料登记流水。同一批次按更正序号认新旧，历史行保留可审计。"""

    __tablename__ = 'coil_registrations'

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), index=True)
    coil_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column()
    created_by: Mapped[int] = mapped_column(ForeignKey('accounts.id'))
    created_at: Mapped[str] = mapped_column(String(32))
