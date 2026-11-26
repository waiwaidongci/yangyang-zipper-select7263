"""Entry points for dataset import, seeding and background execution."""

from collections import defaultdict

from .coils import HOLDOUT, PENDING, TRAIN
from .elastic import fit
from .evaluation import retest_queue, validate_batches
from .fixtures import demo_rows
from .records import normalize_rows
from .reports import point_table, resampling_table, retest_table, validation_tables
from .selection import point_plan
from .settings import FEATURES, SPEC, parse_settings

__all__ = ["SPEC", "demo_rows", "normalize_rows", "analyze", "apply_partition", "effective_splits"]


def effective_splits(rows: list[dict], assignment: dict[str, str] | None) -> dict[str, str]:
    """返回每个批次实际生效的用途；卷号未齐被挂起的批次不在映射内。"""
    batch_splits = {}
    for row in rows:
        batch_splits.setdefault(row["batch_id"], row["split"])
    if not assignment:
        return dict(batch_splits)
    return {batch: assignment[batch] for batch in batch_splits if batch in assignment}


def apply_partition(rows: list[dict], assignment: dict[str, str] | None) -> list[dict]:
    """按卷料防串样划分快照重写实测批用途。

    待检批保持 pending（无破坏峰值，不能进训练/验证侧）；卷号未齐被挂起的
    批次整批剔除，不进入任何计算。
    """
    if not assignment:
        return rows
    result = []
    for row in rows:
        batch = row["batch_id"]
        if row["split"] == PENDING:
            result.append(row)  # 待检批无实测值，永不进训练/验证侧，整批保留用于待检清单。
        elif batch in assignment:
            result.append({**row, "split": assignment[batch]})
        # 其余为卷号未齐的实测批（挂起），整批不入组。
    return result


def analyze(rows: list[dict], settings: dict, split_snapshot: dict | None = None) -> dict:
    options = parse_settings(settings)
    assignment = split_snapshot["assignment"] if split_snapshot else None
    suspended = split_snapshot.get("suspended_batches", []) if split_snapshot else []
    cohorts = defaultdict(list)
    for row in sorted(apply_partition(normalize_rows(rows), assignment),
                      key=lambda row: (row["batch_id"], row["id"])):
        cohorts[row["split"]].append(row)
    train = cohorts[TRAIN]
    holdout = cohorts[HOLDOUT]
    train_batches = {row["batch_id"] for row in train}
    holdout_batches = {row["batch_id"] for row in holdout}
    if len(train_batches) < options["min_train_batches"]:
        raise ValueError(f"训练侧仅 {len(train_batches)} 个批次，未达到约定的 {options['min_train_batches']} 批。")
    if len(holdout_batches) < options["min_holdout_batches"]:
        raise ValueError(
            f"独立验证侧仅 {len(holdout_batches)} 个批次，未达到约定的 {options['min_holdout_batches']} 批。"
        )
    # 整批重采样每轮抽取 B-1 个批次，单个训练批会抽到空样本，无法拟合。
    if len(train_batches) < 2:
        raise ValueError("整批重采样至少需要 2 个训练批，请补齐卷号或放宽划分后重试。")
    plan, trace = point_plan(train, options)
    chosen = tuple(point.feature for point in plan if point.status == "selected")
    used = sum(point.cost for point in plan if point.status == "selected")
    full = fit(train, FEATURES, options["alpha"], options["l1_ratio"])
    trimmed = fit(train, chosen, options["alpha"], options["l1_ratio"])
    comparison, observed = validate_batches(holdout, full, trimmed)
    queue = retest_queue(cohorts[PENDING], trimmed, options["retest_force_n"])
    overall = comparison[0]
    warnings = []
    if not chosen:
        warnings.append("当前频率和布点预算下没有入选测点。独立批对照使用训练均值，待检件暂停排序建议。")
    if overall["mae_delta_n"] > 0.5:
        warnings.append("筛选模型的独立批汇总 MAE 比全测点高逾 0.5 N，请复核省时与误差的取舍。")
    if len(comparison) == 2:
        warnings.append("独立验证目前仅覆盖一个批次，建议结合其他生产批次复核。")
    if not queue:
        warnings.append("本次台账未含待检试片，待检清单为空。")
    if suspended:
        warnings.append(f"卷号未齐的批次暂不入组：{', '.join(suspended)}。")
    coil_block = None
    if split_snapshot:
        coil_block = {
            "coil_version": split_snapshot["coil_version"],
            "train_batches": sorted(train_batches),
            "holdout_batches": sorted(holdout_batches),
            "suspended_batches": suspended,
            "moved_batches": split_snapshot.get("moved_batches", []),
            "components": split_snapshot.get("components", []),
        }
    return {
        "summary": (f"用 {len(train)} 片历史试片完成 {options['resamples']} 轮整批重采样。"
                    f"{len(chosen)} 个测点入选，每片布点 {used}/{options['budget_seconds']} 秒。"
                    f"冻结训练模型后，对照 {len(comparison) - 1} 个独立批共 {overall['rows']} 片的误差。"),
        "metrics": {"入选测点数": len(chosen), "布点时间（秒/片）": used, "预算（秒/片）": options["budget_seconds"],
                    "独立批全测点 MAE（N）": round(overall["full_mae_n"], 6),
                    "独立批筛选测点 MAE（N）": round(overall["selected_mae_n"], 6),
                    "独立批 MAE 增量（N）": round(overall["mae_delta_n"], 6),
                    "优先做破坏试验件数": sum(row["action"] == "优先做破坏试验" for row in queue)},
        "tables": [point_table(plan, full, trimmed, options["budget_seconds"], options["resamples"]),
                   *validation_tables(comparison, observed), retest_table(queue), resampling_table(trace)],
        "warnings": warnings,
        "coil_split": coil_block,
    }
