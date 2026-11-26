from fastapi import APIRouter

from src.controllers.dependencies import Actor, Db, Writer
from src.schemas.coils import CoilRegistrationCreate, CoilRegistrationResult
from src.services import coils

router = APIRouter(prefix='/coils')


@router.get('/registrations')
def registrations(session: Db, actor: Actor):
    return coils.plan_overview(session)


@router.post('/registrations', response_model=CoilRegistrationResult, status_code=201)
def register(payload: CoilRegistrationCreate, session: Db, actor: Writer):
    return coils.register_coils(session, payload, actor)
