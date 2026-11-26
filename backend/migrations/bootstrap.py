from src.config.database import Base, engine
from src.models import account, audit, coil, dataset, review, run, split_plan  # noqa: F401


def bootstrap():
    Base.metadata.create_all(engine)
