from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from app.errors import DomainError
from app.web import page
from . import repo
from .service import adjudicate, compensate, create_chain, import_batch, readback

router = APIRouter()

ROWS = [
    {"key": "MU/UA 共享", "status": "已归并", "value": "1 段"},
    {"key": "来源冲突", "status": "待裁决", "value": "2 份"},
    {"key": "撤销归并", "status": "补偿中", "value": "v3"},
]


@router.get("/codeshare", response_class=HTMLResponse)
def view(request: Request):
    return page(request, "m7.html", "共享航班", "/api/codeshare/import", ROWS)


def _body(payload) -> dict:
    return payload if isinstance(payload, dict) else {}


def _execute(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except DomainError as error:
        return JSONResponse(error.payload(), status_code=error.status)


@router.post("/api/codeshare/import")
def import_endpoint(payload: dict | None = None):
    return _execute(import_batch, _body(payload))


@router.get("/api/codeshare/groups")
def groups_endpoint():
    def load():
        repo.init()
        with repo.reading() as conn:
            return readback(conn)

    return _execute(load)


@router.post("/api/codeshare/groups/{group_id}/adjudicate")
def adjudicate_endpoint(group_id: str, payload: dict | None = None):
    return _execute(adjudicate, group_id, _body(payload))


@router.post("/api/codeshare/groups/{group_id}/compensate")
def compensate_endpoint(group_id: str, payload: dict | None = None):
    return _execute(compensate, group_id, _body(payload))


@router.post("/api/codeshare/chains")
def chain_endpoint(payload: dict | None = None):
    return _execute(create_chain, _body(payload))


@router.get("/api/codeshare/batches/{batch_id}")
def batch_endpoint(batch_id: str):
    def load():
        repo.init()
        with repo.reading() as conn:
            from .service import _batch_view

            return _batch_view(conn, batch_id)

    return _execute(load)
