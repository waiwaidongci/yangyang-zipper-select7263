"""服务层与 worker 编排的离线集成测试：python3 -I tests/test_services.py

通过 tests/stubs 下的替身加载 SQLAlchemy/redis/jwt，验证：
- 卷料登记 -> 划分版本化 -> 未签核运行失效并按新划分重排；
- 已签核运行冻结保留、仍按原版导出；
- 重算途中卷号又变时旧结果不能签核；
- 划分无解返回 SplitConflict 且登记已落库；
- 新运行绑定最新划分快照。
"""

import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "stubs"))
sys.path.insert(1, BACKEND)

from sqlalchemy import reset_store  # noqa: E402

from src.constants.workflow import Decision, RunStatus  # noqa: E402
from src.config.database import Session  # noqa: E402
from src.domain.coils import SplitConflict  # noqa: E402
from src.domain.fixtures import demo_rows  # noqa: E402
from src.domain.project import SPEC  # noqa: E402
from src.models.account import Account  # noqa: E402
from src.models.dataset import Dataset  # noqa: E402
from src.models.review import Review  # noqa: E402
from src.models.run import Run  # noqa: E402
from src.schemas.coils import CoilRegistrationCreate  # noqa: E402
from src.services import coils as coil_service  # noqa: E402
from src.services import reviews, runs  # noqa: E402
from src.services.catalog import add_dataset  # noqa: E402
from src.services.execution import claim, execute, recover_jobs  # noqa: E402
from src.utils.security import hash_password  # noqa: E402
from src.utils.time import utc_stamp  # noqa: E402

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name} {detail}")


class Actor:
    def __init__(self, account_id, role):
        self.id = account_id
        self.role = role


class RunPayload:
    def __init__(self, dataset_id, label="早班 12 秒布点", settings=None):
        self.dataset_id = dataset_id
        self.label = label
        self.settings = settings or {}


def setup_world():
    reset_store()
    analyst = Account(email="analyst@demo.local", role="analyst", password_hash=hash_password("x"))
    admin = Account(email="admin@demo.local", role="admin", password_hash=hash_password("x"))
    session = Session()
    session.add(analyst)
    session.add(admin)
    dataset = add_dataset(session, SPEC["dataset_name"], demo_rows(), analyst.id)
    session.commit()
    return session, analyst, admin, dataset


def first_coil_payload(min_holdout=2):
    entries = []

    def add(batch, coil, revision=1):
        entries.append({"batch_id": batch, "coil_id": coil, "revision": revision})

    splits = {}
    for row in demo_rows():
        splits.setdefault(row["batch_id"], row["split"])
    # T01 与 V01 共用一卷，迫使 V01 挪到训练侧；其余批次独立成卷。
    add("ZP-T01", "SHARED-1")
    add("ZP-V01", "SHARED-1")
    for batch in splits:
        if batch not in ("ZP-T01", "ZP-V01"):
            add(batch, f"R-{batch}")
    return CoilRegistrationCreate(entries=entries, min_train_batches=3, min_holdout_batches=min_holdout)


def drain_queue(session):
    processed = []
    while True:
        job = claim()
        if job is None:
            break
        execute(job)
        processed.append(job[0])
    return processed


def test_full_lifecycle():
    print("生命周期：登记->运行->卷号更新->失效重算->签核冻结")
    session, analyst, admin, dataset = setup_world()

    # 首次登记，生成版本 1 划分。
    outcome = coil_service.register_coils(session, first_coil_payload(), analyst)
    check("首版划分版本号为 1", outcome["coil_version"] == 1)
    check("V01 被挪入训练侧", outcome["plan"]["assignment"]["ZP-V01"] == "train")
    check("独立侧剩余 V02/V03",
          outcome["plan"]["assignment"]["ZP-V02"] == "holdout"
          and outcome["plan"]["assignment"]["ZP-V03"] == "holdout")

    # 分析员发起运行，绑定版本 1 快照。
    info = runs.create_run(session, RunPayload(dataset.id), analyst)
    first_run_id = info["id"]
    check("新运行绑定卷料版本 1", info["coil_version"] == 1, info)
    stored = session.get(Run, first_run_id)
    check("运行冻结划分快照", stored.split_snapshot["assignment"]["ZP-V01"] == "train")

    # worker 计算完成。
    processed = drain_queue(session)
    check("worker 完成首运行", processed == [first_run_id], processed)
    finished = session.get(Run, first_run_id)
    check("首运行完成", finished.status == RunStatus.COMPLETED)
    check("结果按新划分计算", finished.result["coil_split"]["coil_version"] == 1)
    check("独立批排除串样 V01", "ZP-V01" not in finished.result["coil_split"]["holdout_batches"])
    approved_metrics = dict(finished.result["metrics"])

    # 管理员签核版本 1 结果。
    class ReviewPayload:
        decision = Decision.APPROVED
        note = "复核通过"

    reviews.submit(session, first_run_id, ReviewPayload(), admin)
    check("签核记录写入", session.get(Review, first_run_id).decision == Decision.APPROVED)

    # 再发起一个未签核运行（同版本），随后上游卷号更新。
    info2 = runs.create_run(session, RunPayload(dataset.id, label="第二次运行"), analyst)
    second_run_id = info2["id"]
    drain_queue(session)
    check("第二运行完成待签核", session.get(Run, second_run_id).status == RunStatus.COMPLETED)

    # 卷号更新：T05 与 V02 共用一卷，V02 也必须离开独立侧；独立侧只剩 V03，下限放宽到 1。
    update_entries = [
        {"batch_id": "ZP-T05", "coil_id": "SHARED-2", "revision": 1},
        {"batch_id": "ZP-V02", "coil_id": "SHARED-2", "revision": 1},
    ]
    payload_v2 = CoilRegistrationCreate(entries=update_entries, min_train_batches=3, min_holdout_batches=1)
    outcome_v2 = coil_service.register_coils(session, payload_v2, analyst)
    check("划分升到版本 2", outcome_v2["coil_version"] == 2)
    check("V02 在新版本挪到训练侧", outcome_v2["plan"]["assignment"]["ZP-V02"] == "train")

    # 未签核的第二运行失效；已签核的第一运行保持完成状态。
    stale = session.get(Run, second_run_id)
    check("未签核结果失效", stale.status == RunStatus.SUPERSEDED)
    check("失效结果带原因", stale.error and "不可签核" in stale.error)
    approved = session.get(Run, first_run_id)
    check("已签核运行不受影响", approved.status == RunStatus.COMPLETED)
    check("已签核结果原版冻结", approved.result["metrics"] == approved_metrics)
    check("已签核仍可导出原版",
          runs.export_table(session, first_run_id, "point_selection", admin)["key"] == "point_selection")

    # 失效运行被按新划分重排。
    check("恰好一个旧运行被重排", len(outcome_v2["requeued"]) == 1, outcome_v2["requeued"])
    replacement_id = outcome_v2["requeued"][0]
    check("重排的是第二个运行的继任者", replacement_id != second_run_id and replacement_id > second_run_id)
    replacement = session.get(Run, replacement_id)
    check("重排运行绑定版本 2", replacement.coil_version == 2)
    check("重排运行携带新快照", replacement.split_snapshot["assignment"]["ZP-V02"] == "train")
    check("重排运行进入队列", replacement.status == RunStatus.QUEUED)

    # 失效运行不能签核。
    class RejectPayload:
        decision = Decision.APPROVED
        note = "试图签核失效结果"

    blocked = False
    try:
        reviews.submit(session, second_run_id, RejectPayload(), admin)
    except Exception as error:
        blocked = getattr(error, "status_code", None) == 409
    check("失效结果拒绝签核(409)", blocked)

    # worker 重算版本 2，独立批只剩 V03。
    drain_queue(session)
    recalculated = session.get(Run, replacement_id)
    check("重排运行重算完成", recalculated.status == RunStatus.COMPLETED)
    check("重算结果为版本 2", recalculated.result["coil_split"]["coil_version"] == 2)
    check("重算独立批只剩 V03", recalculated.result["coil_split"]["holdout_batches"] == ["ZP-V03"])


def test_change_during_recalculation():
    print("竞态：重算途中卷号又变，旧结果不能签核")
    session, analyst, admin, dataset = setup_world()
    coil_service.register_coils(session, first_coil_payload(), analyst)
    info = runs.create_run(session, RunPayload(dataset.id), analyst)
    run_id = info["id"]

    # worker 领取任务（进入 running），但在写回结果前卷号再次更新。
    job = claim()
    check("任务被领取", job is not None and job[0] == run_id)
    running = session.get(Run, run_id)
    check("领取后为 running", running.status == RunStatus.RUNNING)

    update_entries = [{"batch_id": "ZP-T05", "coil_id": "NEW-COIL", "revision": 1}]
    payload = CoilRegistrationCreate(entries=update_entries, min_train_batches=2, min_holdout_batches=1)
    outcome = coil_service.register_coils(session, payload, analyst)
    check("卷号再次更新到版本 2", outcome["coil_version"] == 2)
    check("在途运行被标记失效", session.get(Run, run_id).status == RunStatus.SUPERSEDED)
    check("在途运行产生重排继任者", len(outcome["requeued"]) == 1 and outcome["requeued"][0] != run_id)

    # worker 此刻才算完并尝试写回：execute 检测到状态已变，放弃写回。
    execute(job)
    stale = session.get(Run, run_id)
    check("旧结果写回被拦截", stale.status == RunStatus.SUPERSEDED and stale.result is None)

    # 重排的新运行正常完成并可签核。
    new_id = outcome["requeued"][0]
    drain_queue(session)
    fresh = session.get(Run, new_id)
    check("新版本运行完成", fresh.status == RunStatus.COMPLETED and fresh.coil_version == 2)

    class ReviewPayload:
        decision = Decision.APPROVED
        note = "ok"

    review = reviews.submit(session, new_id, ReviewPayload(), admin)
    check("新版本结果允许签核", review["decision"] == Decision.APPROVED)


def test_conflict_and_recovery():
    print("冲突与 worker 恢复")
    session, analyst, admin, dataset = setup_world()
    # 所有批次共用一卷 -> 单个连通件，无法同时满足两侧批数。
    entries = [{"batch_id": row["batch_id"], "coil_id": "ONE-COIL", "revision": 1} for row in demo_rows()]
    payload = CoilRegistrationCreate(entries=entries, min_train_batches=3, min_holdout_batches=2)
    conflict = False
    try:
        coil_service.register_coils(session, payload, analyst)
    except SplitConflict:
        conflict = True
    check("无解时抛 SplitConflict", conflict)
    check("冲突时登记仍已落库", len(coil_service.current_registrations(session)) > 0)
    check("冲突不产生划分版本", coil_service.latest_plan(session) is None)

    # 用更正序号 2 把卷号纠正为每批独立卷，划分恢复可解；版本从 1 开始计数。
    corrected = []

    def add(batch, coil, revision):
        corrected.append({"batch_id": batch, "coil_id": coil, "revision": revision})

    add("ZP-T01", "SHARED-1", 2)
    add("ZP-V01", "SHARED-1", 2)
    for row in demo_rows():
        batch = row["batch_id"]
        if batch not in ("ZP-T01", "ZP-V01"):
            add(batch, f"R-{batch}", 2)
    fixed_payload = CoilRegistrationCreate(entries=corrected, min_train_batches=3, min_holdout_batches=2)
    ok = coil_service.register_coils(session, fixed_payload, analyst)
    check("冲突纠正后首版编号为 1", ok["coil_version"] == 1)

    # worker 中断恢复：running 的活跃运行重新排队。
    info = runs.create_run(session, RunPayload(dataset.id), analyst)
    run_id = info["id"]
    claim()
    check("领取后 running", session.get(Run, run_id).status == RunStatus.RUNNING)
    recover_jobs()
    check("恢复后重新排队", session.get(Run, run_id).status == RunStatus.QUEUED)
    drain_queue(session)
    check("恢复后可正常完成", session.get(Run, run_id).status == RunStatus.COMPLETED)


def test_repeated_updates_no_duplicate_successors():
    print("连续卷料更新：不重复失效、不产生重复继任者")
    session, analyst, admin, dataset = setup_world()
    # 卷料登记前先建一个无卷料版本的旧运行（兼容首批登记场景）。
    legacy = runs.create_run(session, RunPayload(dataset.id, label="卷料前旧运行"), analyst)
    check("旧运行无卷料版本", legacy["coil_version"] is None)
    drain_queue(session)

    # 首次登记：旧运行失效，产生继任者 R1。
    first = coil_service.register_coils(session, first_coil_payload(), analyst)
    check("首批登记使旧运行失效", legacy["id"] in first["invalidated"])
    check("旧运行产生一个继任者", len(first["requeued"]) == 1)
    r1_id = first["requeued"][0]
    drain_queue(session)

    # 第二次更新：只失效 R1，已 superseded 的旧运行不再重复产生继任者。
    payload2 = CoilRegistrationCreate(
        entries=[{"batch_id": "ZP-T05", "coil_id": "S2", "revision": 1},
                 {"batch_id": "ZP-V02", "coil_id": "S2", "revision": 1}],
        min_train_batches=3, min_holdout_batches=1,
    )
    second = coil_service.register_coils(session, payload2, analyst)
    check("第二次只失效活跃继任者", second["invalidated"] == [r1_id], second["invalidated"])
    check("第二次只产生一个新继任者", len(second["requeued"]) == 1)
    r2_id = second["requeued"][0]
    check("继任者版本递进", session.get(Run, r2_id).coil_version == 2)

    # 第三次更新：链上仍是一个失效、一个继任，不沿历史 superseded 行复制。
    payload3 = CoilRegistrationCreate(
        entries=[{"batch_id": "ZP-T06", "coil_id": "S3", "revision": 1}],
        min_train_batches=2, min_holdout_batches=1,
    )
    third = coil_service.register_coils(session, payload3, analyst)
    check("第三次只失效当前活跃运行", third["invalidated"] == [r2_id], third["invalidated"])
    check("第三次仍只产生一个继任者", len(third["requeued"]) == 1)
    check("历史失效运行不被重复处理",
          third["invalidated"].count(legacy["id"]) == 0 and third["invalidated"].count(r1_id) == 0)
    drain_queue(session)
    check("最新继任者完成", session.get(Run, third["requeued"][0]).status == RunStatus.COMPLETED)


if __name__ == "__main__":
    test_full_lifecycle()
    test_change_during_recalculation()
    test_conflict_and_recovery()
    test_repeated_updates_no_duplicate_successors()
    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        sys.exit(1)
    print("全部服务层编排验证通过")
