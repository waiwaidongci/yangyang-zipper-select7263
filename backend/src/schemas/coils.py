from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, constr


class CoilEntry(BaseModel):
    model_config = ConfigDict(extra='forbid')
    batch_id: Annotated[str, constr(strip_whitespace=True, min_length=1, max_length=64)]
    coil_id: Annotated[str, constr(strip_whitespace=True, min_length=1, max_length=64)]
    revision: int = Field(ge=1)


class CoilRegistrationCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    entries: list[CoilEntry] = Field(min_length=1, max_length=500)
    min_train_batches: int = Field(default=3, ge=1, le=64)
    min_holdout_batches: int = Field(default=2, ge=1, le=64)


class CoilRegistrationResult(BaseModel):
    coil_version: int
    registered: int
    plan: dict
    invalidated: list[int]
    requeued: list[int]
