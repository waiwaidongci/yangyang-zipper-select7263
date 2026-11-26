"""Coil registration ledger, leakage-safe re-partitioning and run invalidation."""

from sqlalchemy import select

from src.constants.workflow import RunStatus
from src.domain.coils import SplitConflict, latest_registrations, plan_partition, validate_registration
from src.models.coil import CoilRegistration
from src.models.dataset import Dataset
from src.models.review import Review
from src.models.run import Run
from src.models.split_plan import SplitPlan
from src.services.audit import record
from src.services.queue import notify_worker
from src.utils.time import utc_stamp


def all_batches(session) -> dict[str, str]:
    """跨台账汇总每个批次的原始用途（一个批次只归属一种用途，导入时已校验）。"""
    batch_splits: dict[str, str] = {}
    for dataset in session.scalars(select(Dataset)):
        for row in dataset.rows:
            batch_splits.setdefault(row["batch_id"], row["split"])
    return dict(sorted(batch_splits.items()))


def current_registrations(session) -> dict[str, dict]:
    entries = [
        {"batch_id": item.batch_id, "coil_id": item.coil_id, "revision": item.revision}
        for item in session.scalars(select(CoilRegistration).order_by(CoilRegistration.id))
    ]
    return latest_registrations(entries)


def latest_plan(session) -> SplitPlan | None:
    return session.scalar(select(SplitPlan).order_by(SplitPlan.coil_version.desc()).limit(1))


def build_plan(session, version: int, min_train_batches: int, min_holdout_batches: int) -> SplitPlan:
    """按当前卷料登记计算指定版本的划分；无解时抛 SplitConflict，不落库。"""
    batch_splits = all_batches(session)
    current = current_registrations(session)
    partition = plan_partition(batch_splits, current, min_train_batches, min_holdout_batches)
    return SplitPlan(
        coil_version=version,
        plan={**partition, "coil_version": version, "coils": current, "batch_splits": batch_splits,
              "min_train_batches": min_train_batches,
              "min_holdout_batches": min_holdout_batches},
        created_at=utc_stamp(),
    )


def invalidate_stale_runs(session, coil_version: int, split_snapshot: dict) -> dict:
    """未签核结果随卷号更新失效并按新划分重排；已签核运行冻结保留原版。

    已失效（superseded）或已失败（failed）的运行不再重复产生继任者，避免一次卷料
    更新沿旧链路层层复制运行。
    """
    live_statuses = (RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.COMPLETED)
    invalidated, requeued = [], []
    for run in session.scalars(select(Run).order_by(Run.id)):
        review = session.get(Review, run.id)
        if review is not None or run.coil_version == coil_version or run.status not in live_statuses:
            continue
        run.status = RunStatus.SUPERSEDED
        run.error = "上游卷号已更新，本结果按旧划分计算，不可签核；已按新划分重排重算。"
        invalidated.append(run.id)
        # 重排运行沿用原参数，但两侧约定批数以新划分快照为准，避免设置与划分口径打架。
        settings = dict(run.settings)
        settings["min_train_batches"] = split_snapshot["min_train_batches"]
        settings["min_holdout_batches"] = split_snapshot["min_holdout_batches"]
        replacement = Run(
            label=run.label, dataset_id=run.dataset_id, created_by=run.created_by,
            created_at=utc_stamp(), status=RunStatus.QUEUED, settings=settings,
            coil_version=coil_version, split_snapshot=split_snapshot,
        )
        session.add(replacement)
        session.flush()
        record(session, "run.queued", replacement.id, run.created_by)
        requeued.append(replacement.id)
    return {"invalidated": invalidated, "requeued": requeued}


def register_coils(session, payload, actor) -> dict:
    entries = [entry.model_dump() for entry in payload.entries]
    for line, entry in enumerate(entries, 1):
        validate_registration(entry, line)
    known_batches = set(all_batches(session))
    unknown = sorted({entry["batch_id"] for entry in entries if entry["batch_id"] not in known_batches})
    if unknown:
        raise ValueError(f"卷料登记引用了台账中不存在的批次：{', '.join(unknown)}。")
    current_plan = latest_plan(session)
    version = (current_plan.coil_version + 1) if current_plan else 1

    created = []
    for entry in entries:
        item = CoilRegistration(
            batch_id=entry["batch_id"].strip(), coil_id=entry["coil_id"].strip(),
            revision=entry["revision"], created_by=actor.id, created_at=utc_stamp(),
        )
        session.add(item)
        created.append(item)
    session.flush()
    record(session, "coil.registrations", created[0].id, actor.id)

    try:
        plan = build_plan(session, version, payload.min_train_batches, payload.min_holdout_batches)
    except SplitConflict as error:
        record(session, "split.conflict", version, actor.id)
        session.commit()
        raise error
    session.add(plan)
    session.flush()
    record(session, "split.replanned", plan.coil_version, actor.id)
    effect = invalidate_stale_runs(session, plan.coil_version, plan.plan)
    session.commit()
    notify_worker()
    return {"coil_version": plan.coil_version, "registered": len(created), "plan": plan.plan, **effect}


def plan_overview(session) -> dict:
    current = current_registrations(session)
    plan = latest_plan(session)
    return {
        "registrations": [
            {"batch_id": batch, **detail} for batch, detail in sorted(current.items())
        ],
        "latest_plan": None if plan is None else {"coil_version": plan.coil_version, **plan.plan},
    }
