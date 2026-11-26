"""路由端到端流程的离线测试：python3 -I tests/test_routes.py

直接调用 FastAPI 路由函数（替身环境下它们是普通函数），走完整鉴权与服务链路。
"""

import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "stubs"))
sys.path.insert(1, BACKEND)

from sqlalchemy import reset_store  # noqa: E402

from src.config.database import Session  # noqa: E402
from src.constants.workflow import Decision, Role, RunStatus  # noqa: E402
from src.domain.fixtures import demo_rows  # noqa: E402
from src.domain.project import SPEC  # noqa: E402
from src.models.account import Account  # noqa: E402
from src.models.run import Run  # noqa: E402
from src.routes import auth, coils as coil_routes, runs as run_routes  # noqa: E402
from src.schemas.auth import Credentials  # noqa: E402
from src.schemas.coils import CoilRegistrationCreate  # noqa: E402
from src.schemas.reviews import ReviewCreate  # noqa: E402
from src.schemas.runs import RunCreate  # noqa: E402
from src.services.catalog import add_dataset  # noqa: E402
from src.services.execution import claim, execute  # noqa: E402
from src.utils.security import hash_password  # noqa: E402

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name} {detail}")


def drain():
    while True:
        job = claim()
        if job is None:
            break
        execute(job)


def coil_entries():
    entries = []

    def add(b, c, r=1):
        entries.append({"batch_id": b, "coil_id": c, "revision": r})

    add("ZP-T01", "SHARED-1")
    add("ZP-V01", "SHARED-1")
    splits = {}
    for row in demo_rows():
        splits.setdefault(row["batch_id"], row["split"])
    for batch in splits:
        if batch not in ("ZP-T01", "ZP-V01"):
            add(batch, f"R-{batch}")
    return entries


def main():
    print("路由端到端：鉴权->登记->运行->失效->签核->导出")
    reset_store()
    session = Session()
    analyst = Account(email="analyst@demo.local", role=Role.ANALYST, password_hash=hash_password("pw"))
    admin = Account(email="admin@demo.local", role=Role.ADMIN, password_hash=hash_password("pw"))
    viewer = Account(email="viewer@demo.local", role=Role.VIEWER, password_hash=hash_password("pw"))
    for account in (analyst, admin, viewer):
        session.add(account)
    dataset = add_dataset(session, SPEC["dataset_name"], demo_rows(), analyst.id)
    session.commit()

    # 登录拿到 token（替身 jwt 里 token 即账号 id）。
    token = auth.login(Credentials(email="analyst@demo.local", password="pw"), session)["access_token"]
    check("分析员登录签发令牌", token == str(analyst.id))

    # 鉴权依赖在 FastAPI 注入层生效，这里直接验证依赖函数本身的角色判定。
    from src.controllers.dependencies import require_admin, require_writer
    from fastapi import HTTPException
    viewer_blocked = False
    try:
        require_writer(viewer)
    except HTTPException as error:
        viewer_blocked = error.status_code == 403
    check("查阅员被写作依赖拒绝(403)", viewer_blocked)
    analyst_blocked = False
    try:
        require_admin(analyst)
    except HTTPException as error:
        analyst_blocked = error.status_code == 403
    check("分析员被管理员依赖拒绝(403)", analyst_blocked)
    check("管理员通过管理员依赖", require_admin(admin).role == Role.ADMIN)

    # 分析员登记成功。
    payload = CoilRegistrationCreate(entries=coil_entries())
    outcome = coil_routes.register(payload, session, analyst)
    check("登记返回版本 1", outcome["coil_version"] == 1)

    # 任何人可查卷料概览。
    overview = coil_routes.registrations(session, viewer)
    check("查阅员可看卷料概览", overview["latest_plan"]["coil_version"] == 1)

    # 发起运行并由 worker 完成。
    run_info = run_routes.create(RunCreate(label="早班 12 秒布点", dataset_id=dataset.id), session, analyst)
    run_id = run_info["id"]
    check("运行绑定卷料版本", run_info["coil_version"] == 1)
    drain()
    check("运行计算完成", session.get(Run, run_id).status == RunStatus.COMPLETED)

    # 分析员角色不满足管理员依赖（路由层由 require_admin 拦截）。
    forbidden = False
    try:
        require_admin(analyst)
    except HTTPException as error:
        forbidden = error.status_code == 403
    check("分析员签核被管理员依赖拒绝(403)", forbidden)

    # 管理员签核。
    review = run_routes.review(
        run_id, ReviewCreate(decision=Decision.APPROVED, note="复核通过意见"), session, admin
    )
    check("管理员签核成功", review["decision"] == Decision.APPROVED)

    # 已签核可导出原版 CSV。
    response = run_routes.export(run_id, "point_selection", session, viewer)
    check("签核后可导出 CSV", response.media_type == "text/csv")
    check("导出附件含文件名", "run-" in response.headers["Content-Disposition"])

    # 未签核的新运行在卷号更新后失效，且不能签核。
    second = run_routes.create(RunCreate(label="第二次", dataset_id=dataset.id), session, analyst)
    drain()
    update_payload = CoilRegistrationCreate(
        entries=[{"batch_id": "ZP-T05", "coil_id": "S2", "revision": 1},
                 {"batch_id": "ZP-V02", "coil_id": "S2", "revision": 1}],
        min_holdout_batches=1,
    )
    effect = coil_routes.register(update_payload, session, analyst)
    check("卷号更新到版本 2", effect["coil_version"] == 2)
    stale = session.get(Run, second["id"])
    check("未签核运行失效", stale.status == RunStatus.SUPERSEDED)
    cannot_review = False
    try:
        run_routes.review(second["id"], ReviewCreate(decision=Decision.APPROVED, note="试图签核"), session, admin)
    except HTTPException as error:
        cannot_review = error.status_code == 409
    check("失效运行签核返回 409", cannot_review)

    # 已签核的旧运行仍可导出原版（冻结）。
    frozen = run_routes.export(run_id, "point_selection", session, viewer)
    check("已签核报告照原版下载", frozen.body == response.body)

    # 重排继任者完成后，独立批口径变化。
    drain()
    replacement = session.get(Run, effect["requeued"][0])
    check("继任运行重算完成", replacement.status == RunStatus.COMPLETED)
    check("继任结果为版本 2", replacement.result["coil_split"]["coil_version"] == 2)
    check("独立批随划分收缩", replacement.result["coil_split"]["holdout_batches"] == ["ZP-V03"])

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        sys.exit(1)
    print("全部路由端到端流程验证通过")


if __name__ == "__main__":
    main()
