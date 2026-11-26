"""离线测试替身：sqlalchemy.orm 的最小子集。"""

from . import DeclarativeBase as _DeclarativeBase
from . import _Column, _Session, sessionmaker as _sessionmaker


class _Mapped:
    def __getitem__(self, item):
        return item


Mapped = _Mapped()
mapped_column = lambda *args, **kwargs: _Column()
DeclarativeBase = _DeclarativeBase
sessionmaker = _sessionmaker
Session = _Session
