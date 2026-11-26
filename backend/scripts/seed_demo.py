import json

from sqlalchemy import func, select

from migrations.bootstrap import bootstrap
from src.config.database import Session
from src.constants.workflow import Role
from src.domain.project import SPEC, demo_rows
from src.models.account import Account
from src.models.coil import CoilRegistration
from src.models.dataset import Dataset
from src.services.catalog import add_dataset
from src.services.coils import build_plan, latest_plan
from src.utils.security import hash_password
from src.utils.time import utc_stamp


def demo_coil_entries() -> list[dict]:
    """构造含共用卷料与跨卷批次的演示登记，覆盖防串样连通约束。"""
    entries = []

    def add(batch: str, coil: str, revision: int = 1) -> None:
        entries.append({"batch_id": batch, "coil_id": coil, "revision": revision})

    # 训练批：T01/T02 共用 ROLL-A，T03 跨 A、B 两卷，把 A、B 两个卷料族连成一体。
    add("ZP-T01", "ROLL-A")
    add("ZP-T02", "ROLL-A")
    add("ZP-T03", "ROLL-A")
    add("ZP-T03", "ROLL-B")
    add("ZP-T04", "ROLL-B")
    add("ZP-T05", "ROLL-C")
    add("ZP-T06", "ROLL-C")
    for order in range(7, 13):
        add(f"ZP-T{order:02d}", f"ROLL-T{order:02d}")
    # 独立批：V01/V02 共用 ROLL-V1，V03 独立成卷。
    add("ZP-V01", "ROLL-V1")
    add("ZP-V02", "ROLL-V1")
    add("ZP-V03", "ROLL-V3")
    # 待检批共用一卷，连通件整体跟随训练侧。
    add("ZP-P01", "ROLL-P1")
    add("ZP-P02", "ROLL-P1")
    return entries


def seed():
    with Session.begin() as session:
        owner = None
        for role in Role:
            email = f'{role.value}@demo.local'
            account = session.scalar(select(Account).where(Account.email == email))
            if account is None:
                account = Account(email=email, role=role, password_hash=hash_password('Demo-2611!'))
                session.add(account)
                session.flush()
            if role == Role.ADMIN:
                owner = account.id
        name = SPEC['dataset_name']
        dataset = session.scalar(select(Dataset).where(Dataset.name == name))
        if not dataset:
            dataset = add_dataset(session, name, demo_rows(), owner)
        if not session.scalar(select(CoilRegistration).limit(1)):
            for entry in demo_coil_entries():
                session.add(CoilRegistration(**entry, created_by=owner, created_at=utc_stamp()))
            session.flush()
        if latest_plan(session) is None:
            plan = build_plan(session, 1, 3, 2)
            session.add(plan)
            session.flush()
        return {'accounts': session.scalar(select(func.count()).select_from(Account)),
                'datasets': session.scalar(select(func.count()).select_from(Dataset)),
                'coil_registrations': session.scalar(select(func.count()).select_from(CoilRegistration)),
                'demo_rows': len(dataset.rows), 'checksum': dataset.checksum}


if __name__ == '__main__':
    bootstrap()
    print(json.dumps(seed(), ensure_ascii=False))
