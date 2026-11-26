import json
import logging

from sqlalchemy import select, update

from src.config.database import Session
from src.constants.workflow import RunStatus
from src.domain.project import analyze
from src.models.dataset import Dataset
from src.models.run import Run
from src.services.audit import record


def recover_jobs():
    with Session.begin() as session:
        # worker 中断后回收在途运行。卷号更新期间失效的运行已在注册事务里标记
        # superseded（不会停留在 running），因此这里只需把剩余 running 重新排队。
        session.execute(update(Run).where(Run.status == RunStatus.RUNNING).values(status=RunStatus.QUEUED))


def claim():
    with Session.begin() as session:
        run = session.scalar(select(Run).where(Run.status == RunStatus.QUEUED)
                             .order_by(Run.id).limit(1).with_for_update(skip_locked=True))
        if run is None:
            return None
        run.status = RunStatus.RUNNING
        dataset = session.get(Dataset, run.dataset_id)
        record(session, 'run.started', run.id)
        return run.id, dataset.rows, run.settings, run.split_snapshot


def execute(job):
    run_id, rows, settings, split_snapshot = job
    result = None
    error_message = None
    try:
        result = analyze(rows, settings, split_snapshot)
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except ValueError as error:
        result = None
        error_message = str(error)
    except Exception:
        logging.getLogger(__name__).exception('计算运行 %s 失败', run_id)
        result = None
        error_message = '计算遇到内部错误，请检查台账后重试。'
    with Session.begin() as session:
        run = session.get(Run, run_id)
        # 重算途中卷号又变：旧结果即便算完也保持失效，不能签核，新版本会另行重排。
        if run.status != RunStatus.RUNNING:
            record(session, 'run.superseded', run.id)
            return
        run.status = RunStatus.FAILED if error_message is not None else RunStatus.COMPLETED
        run.result = result
        run.error = error_message
        record(session, 'run.' + run.status, run.id)
