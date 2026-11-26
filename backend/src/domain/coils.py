"""Coil (roll material) registration and leakage-safe split planning.

同一卷料分批送检时，迟到的卷号记录会让训练侧与独立验证侧串样。本模块只依赖
标准库，提供三件事：

1. ``latest_registrations``：按更正序号折叠卷料登记，得到每个批次当前认账的卷号；
2. ``build_components``：以"共用卷料"为边合并批次，跨卷批次整体连通；
3. ``plan_partition``：连通件整体落在同一侧，在两侧约定批数下求挪动原批最少的划分。

卷号未齐的批次（缺少有效登记）不会进入任何一侧，先挂起等待卷料补齐。
"""

import re
from collections import defaultdict

from .records import CODE

COIL_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TRAIN = "train"
HOLDOUT = "holdout"
PENDING = "pending"


class SplitConflict(Exception):
    """不存在满足约定批数与连通约束的划分。"""


def parse_coil_code(value, line: int = 0) -> str:
    if isinstance(value, str) and COIL_CODE.fullmatch(value.strip()):
        return value.strip()
    raise ValueError(f"第 {line} 条卷料登记的卷号须为 1 至 64 位编号，以英文字母或数字开头。")


def validate_registration(entry: dict, line: int = 0) -> dict:
    """校验单条卷料登记，返回规范化后的字典。"""
    if not isinstance(entry, dict):
        raise ValueError(f"第 {line} 条卷料登记须为对象。")
    missing = {"batch_id", "coil_id", "revision"} - entry.keys()
    if missing:
        raise ValueError(f"第 {line} 条卷料登记缺少字段：{', '.join(sorted(missing))}。")
    batch_id = entry["batch_id"]
    if not isinstance(batch_id, str) or not CODE.fullmatch(batch_id.strip()):
        raise ValueError(f"第 {line} 条卷料登记的拉伸批次编号不合法。")
    coil_id = parse_coil_code(entry["coil_id"], line)
    revision = entry["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError(f"第 {line} 条卷料登记的更正序号须为不小于 1 的整数。")
    return {"batch_id": batch_id.strip(), "coil_id": coil_id, "revision": revision}


def latest_registrations(entries: list[dict]) -> dict[str, dict]:
    """按批次折叠登记：更正序号认新旧，取每批最大序号对应的全部卷号。

    一个批次可跨多个卷号（同序号多条登记）；迟到的高序号登记整体覆盖旧声明。
    返回 ``{batch_id: {"coil_ids": [...], "revision": n}}``，卷号按字典序去重。
    """
    max_revision: dict[str, int] = {}
    for line, raw in enumerate(entries, 1):
        entry = validate_registration(raw, line)
        max_revision[entry["batch_id"]] = max(max_revision.get(entry["batch_id"], 0), entry["revision"])
    current: dict[str, dict] = {}
    for line, raw in enumerate(entries, 1):
        entry = validate_registration(raw, line)
        if entry["revision"] != max_revision[entry["batch_id"]]:
            continue
        known = current.setdefault(entry["batch_id"], {"coil_ids": [], "revision": entry["revision"]})
        if entry["coil_id"] not in known["coil_ids"]:
            known["coil_ids"].append(entry["coil_id"])
    for known in current.values():
        known["coil_ids"].sort()
    return current


def build_components(batch_splits: dict[str, str], current: dict[str, dict]) -> list[dict]:
    """并查集合并批次：卷号未齐的批次挂起，其余按共用卷号连成连通件。

    跨卷批次（一个批次登记多个卷号时由多条边表达）与其邻批整体连通。
    返回连通件列表，每个含成员批次、各侧原批数与是否含待检批。
    """
    eligible = sorted(batch for batch in batch_splits if batch in current)
    parent = {batch: batch for batch in eligible}

    def find(batch: str) -> str:
        while parent[batch] != batch:
            parent[batch] = parent[parent[batch]]
            batch = parent[batch]
        return batch

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    coil_owners: dict[str, list[str]] = defaultdict(list)
    for batch in eligible:
        for coil_id in current[batch]["coil_ids"]:
            coil_owners[coil_id].append(batch)
    for owners in coil_owners.values():
        for other in owners[1:]:
            union(owners[0], other)

    grouped: dict[str, list[str]] = defaultdict(list)
    for batch in eligible:
        grouped[find(batch)].append(batch)

    components = []
    for members in grouped.values():
        members.sort()
        components.append({
            "batches": tuple(members),
            "train_batches": tuple(b for b in members if batch_splits[b] == TRAIN),
            "holdout_batches": tuple(b for b in members if batch_splits[b] == HOLDOUT),
            "pending_batches": tuple(b for b in members if batch_splits[b] == PENDING),
        })
    components.sort(key=lambda part: part["batches"])
    return components


def _component_side_cost(part: dict, side: str) -> int:
    """把连通件放到某一侧时，需要挪动的原批数。"""
    if side == TRAIN:
        return len(part["holdout_batches"])
    return len(part["train_batches"])


def plan_partition(
    batch_splits: dict[str, str],
    current: dict[str, dict],
    min_train_batches: int,
    min_holdout_batches: int,
) -> dict:
    """求挪动原批最少的训练/独立验证划分。

    待检（pending）批没有破坏峰值，永远不进训练/验证侧，也不占约定批数；但其
    连通件内的实测批次仍须整体同侧。连通件按成员批次字典序排序后做确定性 DFS，
    平分时按最小字典序解。找不到满足两侧批数下限的划分时抛 :class:`SplitConflict`。
    """
    components = build_components(batch_splits, current)
    # 仅含待检批的连通件不参与划分；含实测批的连通件按实测批计批数与挪动。
    assignable = [part for part in components if part["train_batches"] or part["holdout_batches"]]
    eligible_batches = {batch for part in components for batch in part["batches"]}
    grouped_train = sum(len(part["train_batches"]) for part in assignable)
    grouped_holdout = sum(len(part["holdout_batches"]) for part in assignable)

    if grouped_train < min_train_batches or grouped_holdout < min_holdout_batches:
        raise SplitConflict(
            f"卷号齐备的批次中训练侧 {grouped_train} 批、独立验证侧 {grouped_holdout} 批，"
            f"未达到约定的各 {min_train_batches}/{min_holdout_batches} 批，无法成组。"
        )

    best: dict | None = None

    def search(index: int, train_count: int, holdout_count: int, moved: int,
               sides: list[str], parts: list[dict]) -> None:
        nonlocal best
        if best is not None and moved >= best["moved_batches"]:
            return
        remaining_capacity = sum(
            len(parts[i]["train_batches"]) + len(parts[i]["holdout_batches"]) for i in range(index, len(parts))
        )
        if train_count + remaining_capacity < min_train_batches:
            return
        if index == len(parts):
            if train_count < min_train_batches or holdout_count < min_holdout_batches:
                return
            candidate_sides = tuple(sides)
            if best is None or moved < best["moved_batches"] or (
                moved == best["moved_batches"] and candidate_sides < best["component_sides"]
            ):
                best = {
                    "component_sides": candidate_sides,
                    "moved_batches": moved,
                    "train_count": train_count,
                    "holdout_count": holdout_count,
                }
            return
        part = parts[index]
        size = len(part["train_batches"]) + len(part["holdout_batches"])
        # 先尝试训练侧：字典序 TRAIN < HOLDOUT，保证平分时取最小字典序。
        sides.append(TRAIN)
        search(index + 1, train_count + size, holdout_count,
               moved + _component_side_cost(part, TRAIN), sides, parts)
        sides.pop()
        sides.append(HOLDOUT)
        search(index + 1, train_count, holdout_count + size,
               moved + _component_side_cost(part, HOLDOUT), sides, parts)
        sides.pop()

    search(0, 0, 0, 0, [], assignable)
    if best is None:  # 理论上前置下限检查已覆盖，保留兜底。
        raise SplitConflict(
            "共用卷料把批次连成的连通件无法同时满足两侧约定批数，请放宽批数下限或补齐卷号。"
        )

    assignment: dict[str, str] = {}
    component_trace = []
    for part, side in zip(assignable, best["component_sides"]):
        for batch in (*part["train_batches"], *part["holdout_batches"]):
            assignment[batch] = side
        component_trace.append({
            "batches": list(part["batches"]), "side": side,
            "train_batches": list(part["train_batches"]),
            "holdout_batches": list(part["holdout_batches"]),
            "pending_batches": list(part["pending_batches"]),
        })

    suspended = sorted(batch for batch in batch_splits if batch not in eligible_batches)
    moved = sorted(
        batch for batch, side in assignment.items() if batch_splits[batch] != side
    )
    return {
        "assignment": assignment,
        "components": component_trace,
        "suspended_batches": suspended,
        "moved_batches": moved,
        "train_batch_count": best["train_count"],
        "holdout_batch_count": best["holdout_count"],
        "min_train_batches": min_train_batches,
        "min_holdout_batches": min_holdout_batches,
    }
