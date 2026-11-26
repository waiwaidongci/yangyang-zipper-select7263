"""卷料防串样领域规则的独立验证，仅依赖标准库：python3 -I tests/test_coils.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.domain.coils import (  # noqa: E402
    SplitConflict,
    build_components,
    latest_registrations,
    plan_partition,
    validate_registration,
)
from src.domain.fixtures import demo_rows  # noqa: E402
from src.domain.project import analyze, apply_partition  # noqa: E402

HOLDOUT = "holdout"
TRAIN = "train"
PENDING = "pending"
failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name} {detail}")


def demo_batch_splits():
    splits = {}
    for row in demo_rows():
        splits.setdefault(row["batch_id"], row["split"])
    return splits


# 规则 1：卷料登记按更正序号认新旧，迟到的高序号覆盖旧卷号。
def test_revision_ordering():
    print("规则1 更正序号认新旧")
    entries = [
        {"batch_id": "B1", "coil_id": "OLD", "revision": 1},
        {"batch_id": "B1", "coil_id": "NEW", "revision": 2},
        {"batch_id": "B1", "coil_id": "LATE", "revision": 3},
        {"batch_id": "B2", "coil_id": "K1", "revision": 1},
        {"batch_id": "B2", "coil_id": "K2", "revision": 1},  # 同序号多卷号=跨卷批次
    ]
    current = latest_registrations(entries)
    check("高序号覆盖低序号", current["B1"]["coil_ids"] == ["LATE"] and current["B1"]["revision"] == 3)
    check("同序号多卷号保留为跨卷", current["B2"]["coil_ids"] == ["K1", "K2"])
    stale = latest_registrations([entries[0], entries[1]])
    check("按序到达也认最大序号", stale["B1"]["coil_ids"] == ["NEW"])
    try:
        validate_registration({"batch_id": "B1", "coil_id": "C1", "revision": 0})
        check("更正序号必须>=1", False)
    except ValueError:
        check("更正序号必须>=1", True)
    for bad in (
        {"batch_id": "B1", "coil_id": "C1"},  # 缺更正序号
        {"batch_id": "B1", "coil_id": "C1", "revision": "1"},  # 序号非整数
        {"batch_id": "B1", "coil_id": "-BAD", "revision": 1},  # 卷号非法开头
        {"batch_id": "坏批次", "coil_id": "C1", "revision": 1},  # 批次编号非法
        "not-a-dict",
    ):
        try:
            validate_registration(bad, 1)
            check(f"畸形登记被拒: {bad}", False)
        except ValueError:
            check(f"畸形登记被拒: {bad}", True)


# 规则 2：卷号没齐的批次先不入组（挂起，不进任何一侧）。
def test_suspended_batches():
    print("规则2 卷号未齐挂起")
    splits = {"T1": TRAIN, "T2": TRAIN, "T3": TRAIN, "V1": HOLDOUT, "V2": HOLDOUT, "P1": PENDING}
    current = latest_registrations([
        {"batch_id": "T1", "coil_id": "C1", "revision": 1},
        {"batch_id": "T2", "coil_id": "C2", "revision": 1},
        {"batch_id": "T3", "coil_id": "C3", "revision": 1},
        {"batch_id": "V1", "coil_id": "C4", "revision": 1},
        # V2、P1 没有登记
    ])
    plan = plan_partition(splits, current, 3, 1)
    check("未登记批次挂起", plan["suspended_batches"] == ["P1", "V2"], plan["suspended_batches"])
    check("挂起批次不参与分配", "V2" not in plan["assignment"] and "P1" not in plan["assignment"])
    check("已登记批次都有归属", set(plan["assignment"]) == {"T1", "T2", "T3", "V1"})


# 规则 3：共用卷料的批次留同一侧，跨卷批次连着算。
def test_shared_coil_components():
    print("规则3 共用卷料与跨卷连通")
    splits = {f"B{i}": TRAIN for i in range(1, 7)}
    splits.update({"V1": HOLDOUT, "V2": HOLDOUT, "V3": HOLDOUT})
    current = latest_registrations([
        {"batch_id": "B1", "coil_id": "RA", "revision": 1},
        {"batch_id": "B2", "coil_id": "RA", "revision": 1},       # 与 B1 共用 RA
        {"batch_id": "B3", "coil_id": "RA", "revision": 1},       # 跨卷：RA + RB
        {"batch_id": "B3", "coil_id": "RB", "revision": 1},
        {"batch_id": "B4", "coil_id": "RB", "revision": 1},       # B1-B4 经 B3 全部连通
        {"batch_id": "B5", "coil_id": "RC", "revision": 1},
        {"batch_id": "B6", "coil_id": "RD", "revision": 1},
        {"batch_id": "V1", "coil_id": "RV", "revision": 1},
        {"batch_id": "V2", "coil_id": "RV", "revision": 1},
        {"batch_id": "V3", "coil_id": "RW", "revision": 1},
    ])
    components = build_components(splits, current)
    big = next(part for part in components if "B1" in part["batches"])
    check("跨卷把卷料族连成一个连通件", list(big["batches"]) == ["B1", "B2", "B3", "B4"], big["batches"])
    plan = plan_partition(splits, current, 3, 2)
    sides = plan["assignment"]
    check("连通件整体同侧", len({sides[b] for b in ("B1", "B2", "B3", "B4")}) == 1)
    check("共用卷的 V1/V2 同侧", sides["V1"] == sides["V2"])


# 规则 4：两侧都够约定批数、挪动原批最少、确定性、无解报冲突。
def test_quota_and_min_moves():
    print("规则4 约定批数/最少挪动/确定性/冲突")
    splits = {f"T{i}": TRAIN for i in range(1, 7)}
    splits.update({"V1": HOLDOUT, "V2": HOLDOUT})
    # 每个批次独立卷号，连通件即单批。
    current = latest_registrations([
        {"batch_id": b, "coil_id": f"R-{b}", "revision": 1} for b in splits
    ])
    plan_a = plan_partition(splits, current, 3, 2)
    plan_b = plan_partition(splits, current, 3, 2)
    check("结果确定可复现", plan_a["assignment"] == plan_b["assignment"])
    check("训练侧不少于约定", plan_a["train_batch_count"] >= 3)
    check("验证侧不少于约定", plan_a["holdout_batch_count"] >= 2)
    # 7 个可分配批，下限 3+2=5；最优解应保留尽量多原批 -> 挪动 0 可行。
    check("原批可满足时零挪动", plan_a["moved_batches"] == [], plan_a["moved_batches"])

    # 连通件迫使挪动：T1/T2/T3 与 V1 共用一卷成一件（4 批），其余独立。
    current2 = latest_registrations([
        {"batch_id": "T1", "coil_id": "RX", "revision": 1},
        {"batch_id": "T2", "coil_id": "RX", "revision": 1},
        {"batch_id": "T3", "coil_id": "RX", "revision": 1},
        {"batch_id": "V1", "coil_id": "RX", "revision": 1},
        *({"batch_id": b, "coil_id": f"R-{b}", "revision": 1} for b in ("T4", "T5", "T6", "V2")),
    ])
    plan2 = plan_partition(splits, current2, 3, 2)
    sides = plan2["assignment"]
    check("大连通件整体落在一侧", len({sides[b] for b in ("T1", "T2", "T3", "V1")}) == 1)
    # 大连通件 4 批：整体入训练侧则 V1 挪动；为满足验证侧 2 批，独立的 V2 之外还需
    # 再挪一个训练批（T6）过去，故最少挪动 2 批。
    check("最少挪动为 2 批", plan2["moved_batches"] == ["T6", "V1"], plan2["moved_batches"])
    check("验证侧达到 2 批", plan2["holdout_batch_count"] == 2)
    check("训练侧达到 3 批", plan2["train_batch_count"] >= 3)

    # 无解：所有批次被一卷连成单件，无法同时在两侧放批。
    current3 = latest_registrations([
        {"batch_id": b, "coil_id": "ONE", "revision": 1} for b in splits
    ])
    try:
        plan_partition(splits, current3, 3, 2)
        check("无解时抛冲突", False)
    except SplitConflict:
        check("无解时抛冲突", True)

    # 卷号齐备批数不足约定下限也报冲突。
    try:
        plan_partition({"T1": TRAIN, "V1": HOLDOUT}, current, 3, 2)
        check("批数不足报冲突", False)
    except SplitConflict:
        check("批数不足报冲突", True)


# 规则 5/7：按划分快照重训重算独立批误差；快照冻结后结果稳定（已签核原版）。
def test_analyze_with_snapshot():
    print("规则5/7 按新划分重训与快照冻结")
    rows = demo_rows()
    splits = {}
    for row in rows:
        splits.setdefault(row["batch_id"], row["split"])
    # 让 V01 与训练批 T01 共用一卷，迫使其挪到训练侧；V02/V03 留独立验证。
    entries = [
        {"batch_id": "ZP-T01", "coil_id": "SHARED", "revision": 1},
        {"batch_id": "ZP-V01", "coil_id": "SHARED", "revision": 1},
    ]
    for batch, split in splits.items():
        if batch not in ("ZP-T01", "ZP-V01"):
            entries.append({"batch_id": batch, "coil_id": f"R-{batch}", "revision": 1})
    current = latest_registrations(entries)
    plan = plan_partition(splits, current, 3, 2)
    check("共用卷批次同侧", plan["assignment"]["ZP-T01"] == plan["assignment"]["ZP-V01"])
    check("V01 被挪入训练侧", plan["assignment"]["ZP-V01"] == TRAIN, plan["assignment"]["ZP-V01"])
    check("V01 记为挪动批", "ZP-V01" in plan["moved_batches"])

    snapshot = {**plan, "coil_version": 1}
    result_v1 = analyze(rows, {}, snapshot)
    holdout_batches = result_v1["coil_split"]["holdout_batches"]
    check("独立批不再含串样批次", "ZP-V01" not in holdout_batches, holdout_batches)
    check("挂起列表为空（全部登记）", result_v1["coil_split"]["suspended_batches"] == [])

    # 同一快照重复计算，结果完全一致 -> 已签核报告可照原版重现。
    result_v1_again = analyze(rows, {}, snapshot)
    check("冻结快照结果可重现", result_v1_again["metrics"] == result_v1["metrics"])
    check("结果含卷料划分块", result_v1["coil_split"]["coil_version"] == 1)

    # 卷号更新：T05 与 V02 共用一卷，重划后 V02 也挪到训练侧，独立批只剩 V03。
    entries_v2 = entries + [
        {"batch_id": "ZP-T05", "coil_id": "SHARED2", "revision": 1},
        {"batch_id": "ZP-V02", "coil_id": "SHARED2", "revision": 1},
    ]
    current_v2 = latest_registrations(entries_v2)
    plan_v2 = plan_partition(splits, current_v2, 3, 1)
    snapshot_v2 = {**plan_v2, "coil_version": 2}
    result_v2 = analyze(rows, {"min_holdout_batches": 1}, snapshot_v2)
    check("新划分 V02 离开独立侧", "ZP-V02" not in result_v2["coil_split"]["holdout_batches"])
    check("版本号随卷料更新", result_v2["coil_split"]["coil_version"] == 2)
    check("新旧独立批误差口径不同",
          result_v2["metrics"]["独立批全测点 MAE（N）"] != result_v1["metrics"]["独立批全测点 MAE（N）"]
          or result_v2["coil_split"]["holdout_batches"] != result_v1["coil_split"]["holdout_batches"])

    # 挂起批次的试片不进入计算数据。
    partial = latest_registrations([e for e in entries if e["batch_id"] != "ZP-V03"])
    plan_susp = plan_partition(splits, partial, 3, 1)
    check("缺登记批次挂起", "ZP-V03" in plan_susp["suspended_batches"])
    used = apply_partition(rows, plan_susp["assignment"])
    check("挂起批次试片被排除", all(row["batch_id"] != "ZP-V03" for row in used))
    pending_in_plan = [b for b, s in splits.items() if s == PENDING]
    check("待检批不进训练/验证分配", all(b not in plan_susp["assignment"] for b in pending_in_plan))
    check("待检试片保留为待检", {row["batch_id"] for row in used if row["split"] == PENDING} == set(pending_in_plan))
    check("其余试片保留", len(used) == len(rows) - 10)

    # 单训练批无法整批重采样，analyze 必须给清晰错误而不是除零。
    single = {"assignment": {"ZP-T01": TRAIN, "ZP-V01": HOLDOUT, "ZP-V02": HOLDOUT},
              "suspended_batches": [], "moved_batches": [], "components": [], "coil_version": 3,
              "min_train_batches": 1, "min_holdout_batches": 2}
    try:
        analyze(rows, {"min_train_batches": 1, "min_holdout_batches": 2}, single)
        check("单训练批拒绝重采样", False)
    except ValueError as error:
        check("单训练批拒绝重采样", "2 个训练批" in str(error))


if __name__ == "__main__":
    for test in (test_revision_ordering, test_suspended_batches, test_shared_coil_components,
                 test_quota_and_min_moves, test_analyze_with_snapshot):
        test()
    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        sys.exit(1)
    print("全部卷料防串样规则验证通过")
