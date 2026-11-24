from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from app.db import AlreadyCommitted, db, write_tx
from app.services import core
from app.services import sessions as svc

router = APIRouter()


class OpenIn(BaseModel):
    桩位编号: int
    枪线编号: int | None = None


class PulseIn(BaseModel):
    电量: float = Field(gt=0)
    功率: float = 0
    幂等令牌: str | None = None


class AbortIn(BaseModel):
    原因: str = "急停"


@router.get("/会话")
def list_sessions(状态: str | None = None):
    with db() as conn:
        return core.list_sessions(conn, 状态)


@router.get("/会话/{session_id}")
def get_session(session_id: int):
    with db() as conn:
        d = core.session_detail(conn, session_id)
        if not d:
            raise HTTPException(404, detail="会话不存在")
        return d


@router.post("/开会话")
def open_session(body: OpenIn):
    with write_tx() as conn:
        return svc.open_session(conn, body.桩位编号, body.枪线编号)


@router.post("/会话/{session_id}/脉冲")
def add_pulse(session_id: int, body: PulseIn):
    # 写口统一 IMMEDIATE 事务：超时标与脉冲并发时由 SQLite 写锁串行化，半截即回滚
    try:
        with write_tx() as conn:
            return svc.add_pulse(conn, session_id, body.电量, body.功率, body.幂等令牌)
    except AlreadyCommitted as e:
        # 拒收等场景：审计已在事务内提交落盘，此处仅回传失败回执
        raise HTTPException(e.status_code, detail=e.detail)


@router.post("/会话/{session_id}/结算")
def settle(session_id: int):
    with write_tx() as conn:
        return svc.settle(conn, session_id)


@router.post("/会话/{session_id}/超时")
def timeout(session_id: int):
    with write_tx() as conn:
        return svc.mark_timeout(conn, session_id)


@router.post("/会话/{session_id}/中断")
def abort(session_id: int, body: AbortIn | None = None):
    with write_tx() as conn:
        reason = body.原因 if body else "急停"
        return svc.abort_session(conn, session_id, reason)


@router.get("/脉冲审计")
def pulse_audit(会话编号: int | None = None):
    with db() as conn:
        return core.list_pulse_audit(conn, 会话编号)


@router.get("/超时附加账")
def append_ledger(会话编号: int | None = None):
    with db() as conn:
        return core.list_append_ledger(conn, 会话编号)


@router.post("/超时附加账/{ledger_id}/重放")
def replay_append(ledger_id: int):
    with write_tx() as conn:
        return svc.replay_append(conn, ledger_id)
