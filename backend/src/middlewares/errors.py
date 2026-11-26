from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from src.domain.coils import SplitConflict


def invalid_data(_: Request, error: ValueError):
    return JSONResponse({'detail': str(error)}, status_code=422)


def missing_data(_: Request, error: LookupError):
    return JSONResponse({'detail': str(error)}, status_code=404)


def split_conflict(_: Request, error: SplitConflict):
    return JSONResponse({'detail': str(error)}, status_code=409)


def data_conflict(_: Request, error: IntegrityError):
    return JSONResponse({'detail': '该台账名称或记录已经存在，请刷新后检查'}, status_code=409)
