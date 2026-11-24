from __future__ import annotations
from fastapi import HTTPException

from app.db import AlreadyCommitted
from app.services.core import current_band_price, get_setting, is_day_sealed, now_str, today

# 臂B（超时附加账承接）全局唯一承接臂，禁止与「并入原会话」混用。
# 命运枚举：并入原会话 / 附加账承接 / 拒收 / 重放承接
DISPOSITION_MERGED = "并入原会话"
DISPOSITION_LEDGER = "附加账承接"
DISPOSITION_REJECTED = "拒收"
DISPOSITION_REPLAY = "重放承接"


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


def _next_ledger_no(conn, slip_no: str) -> str:
    n = int(get_setting(conn, "附加账序号", "0")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('附加账序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"{slip_no}-附{n}"


def _find_open_audit(conn, session_id: int, client_token: str | None):
    if not client_token:
        return None
    return conn.execute(
        "SELECT * FROM pulse_audit WHERE session_id=? AND client_token=? ORDER BY id DESC LIMIT 1",
        (session_id, client_token),
    ).fetchone()


def open_session(conn, pile_id: int, gun_id: int | None = None) -> dict:
    if is_day_sealed(conn):
        raise HTTPException(400, detail="充日已封印，禁止新开充")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    if pile["status"] != "空闲":
        raise HTTPException(400, detail=f"桩位当前为{pile['status']}，不可开充")
    site = conn.execute("SELECT * FROM sites WHERE id=?", (pile["site_id"],)).fetchone()
    if not site or not site["active"]:
        raise HTTPException(400, detail="站点未启用")

    if gun_id is None:
        gun = conn.execute(
            "SELECT * FROM guns WHERE pile_id=? AND status='可用' ORDER BY id LIMIT 1",
            (pile_id,),
        ).fetchone()
    else:
        gun = conn.execute(
            "SELECT * FROM guns WHERE id=? AND pile_id=?", (gun_id, pile_id)
        ).fetchone()
    if not gun:
        raise HTTPException(400, detail="无可用枪线")
    if gun["status"] != "可用":
        raise HTTPException(400, detail=f"枪线当前为{gun['status']}")

    price = current_band_price(conn, pile["rate_plan_id"])
    slip = _next_slip(conn)
    ts = now_str()
    cur = conn.execute(
        """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,status,kwh,amount,price_locked,started_at,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            slip,
            pile["site_id"],
            pile_id,
            gun["id"],
            pile["rate_plan_id"],
            today(),
            "充电中",
            0,
            0,
            price,
            ts,
            "",
        ),
    )
    sid = cur.lastrowid
    conn.execute("UPDATE piles SET status='占用' WHERE id=?", (pile_id,))
    conn.execute("UPDATE guns SET status='占用' WHERE id=?", (gun["id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (sid, "开会话", ts, f"锁定电价{price}"),
    )
    return {"会话号": slip, "会话编号": sid, "锁定电价": price, "状态": "充电中"}


def _write_audit(conn, *, session_id, slip_no, delta_kwh, power_kw, disposition,
                 ledger_id=None, pulse_id=None, client_token=None,
                 replayed_of=None, http_status=200, note=""):
    ts = now_str()
    cur = conn.execute(
        """INSERT INTO pulse_audit(session_id,slip_no,delta_kwh,power_kw,disposition,
               ledger_id,pulse_id,client_token,replayed_of,http_status,created_at,note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (session_id, slip_no, delta_kwh, power_kw, disposition, ledger_id, pulse_id,
         client_token, replayed_of, http_status, ts, note),
    )
    return cur.lastrowid


def _merge_pulse(conn, se, delta_kwh, power_kw, ts, client_token):
    """正常路径：脉冲并入在充会话，累计电量与预估金额同刷。"""
    session_id = se["id"]
    cur = conn.execute(
        "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at) VALUES(?,?,?,?)",
        (session_id, delta_kwh, power_kw, ts),
    )
    pulse_id = cur.lastrowid
    new_kwh = float(se["kwh"]) + float(delta_kwh)
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(new_kwh * price, 2)
    conn.execute(
        "UPDATE sessions SET kwh=?, amount=? WHERE id=?",
        (round(new_kwh, 3), amount, session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "脉冲", ts, f"+{delta_kwh}"),
    )
    audit_id = _write_audit(
        conn, session_id=session_id, slip_no=se["slip_no"], delta_kwh=delta_kwh,
        power_kw=power_kw, disposition=DISPOSITION_MERGED, pulse_id=pulse_id,
        client_token=client_token, note="在充会话并入",
    )
    return {
        "会话编号": session_id, "状态": se["status"], "承接": DISPOSITION_MERGED,
        "累计电量": round(new_kwh, 3), "预估金额": amount, "审计编号": audit_id,
    }


def _append_to_ledger(conn, se, delta_kwh, power_kw, ts, client_token, *,
                      replayed_of=None, disposition=DISPOSITION_LEDGER, note=""):
    """臂B 唯一超时承接路径：写附加账并关联原会话；原会话电量不增、状态不改回。"""
    session_id = se["id"]
    slip_no = se["slip_no"]
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(float(delta_kwh) * price, 2)
    ledger_no = _next_ledger_no(conn, slip_no)
    cur = conn.execute(
        """INSERT INTO timeout_append_ledger(ledger_no,session_id,slip_no,delta_kwh,power_kw,
               price_locked,amount,status,replayed,client_token,created_at,note)
           VALUES(?,?,?,?,?,?,?,?,0,?,?,?)""",
        (ledger_no, session_id, slip_no, delta_kwh, power_kw, price, amount,
         "待承接", client_token, ts, note or "超时后脉冲附加账承接"),
    )
    ledger_id = cur.lastrowid
    audit_id = _write_audit(
        conn, session_id=session_id, slip_no=slip_no, delta_kwh=delta_kwh,
        power_kw=power_kw, disposition=disposition, ledger_id=ledger_id,
        client_token=client_token, replayed_of=replayed_of,
        note=note or "会话已超时，落入附加账，原会话电量不增",
    )
    # 回填审计关联，闭合「附加账有行 ↔ 审计有关联」
    conn.execute(
        "UPDATE timeout_append_ledger SET pulse_audit_id=? WHERE id=?", (audit_id, ledger_id)
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "超时附加脉冲", ts, f"{ledger_no} +{delta_kwh}（不增原会话电量）"),
    )
    return {
        "会话编号": session_id, "状态": se["status"], "承接": disposition,
        "附加账编号": ledger_id, "附加账号": ledger_no, "附加电量": float(delta_kwh),
        "附加金额": amount, "原会话累计电量": float(se["kwh"]), "审计编号": audit_id,
    }


def add_pulse(conn, session_id: int, delta_kwh: float, power_kw: float = 0,
              client_token: str | None = None) -> dict:
    if delta_kwh <= 0:
        # 非法脉冲也留审计，保证写口命运全可追溯
        raise HTTPException(400, detail="脉冲电量须大于零")
    # IMMEDIATE 事务内加行锁，杜绝与超时标的并发半截；状态以锁内重读为准
    conn.execute("SELECT 1 FROM sessions WHERE id=? LIMIT 1", (session_id,))
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")

    # 幂等：同 client_token 的脉冲直接回放首次结果，不重复落账
    dup = _find_open_audit(conn, session_id, client_token)
    if dup:
        return {"会话编号": session_id, "承接": dup["disposition"],
                "审计编号": dup["id"], "幂等重放": True,
                "附加账编号": dup["ledger_id"], "脉冲编号": dup["pulse_id"]}

    ts = now_str()
    if se["status"] == "充电中":
        return _merge_pulse(conn, se, delta_kwh, power_kw, ts, client_token)

    if se["status"] == "已超时":
        # 臂B：整笔落附加账，原会话保持已超时、累计电量不变
        return _append_to_ledger(conn, se, delta_kwh, power_kw, ts, client_token)

    # 已关账/已中断/结算中：不承接，整笔拒收；审计必须落盘可追溯，故提交后再上抛
    audit_id = _write_audit(
        conn, session_id=session_id, slip_no=se["slip_no"], delta_kwh=delta_kwh,
        power_kw=power_kw, disposition=DISPOSITION_REJECTED, client_token=client_token,
        http_status=400, note=f"会话状态为{se['status']}，脉冲拒收",
    )
    raise AlreadyCommitted(
        400, {"消息": f"会话状态为{se['status']}，脉冲整笔拒收", "审计编号": audit_id}
    )


def replay_append(conn, ledger_id: int) -> dict:
    """附加账脉冲命运可重放：仅标记重放轨迹，绝不把原会话改回充电中、不增原会话电量。"""
    conn.execute("SELECT 1 FROM timeout_append_ledger WHERE id=? LIMIT 1", (ledger_id,))
    row = conn.execute(
        "SELECT * FROM timeout_append_ledger WHERE id=?", (ledger_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, detail="附加账不存在")
    se = conn.execute(
        "SELECT * FROM sessions WHERE id=?", (row["session_id"],)
    ).fetchone()
    if not se:
        raise HTTPException(404, detail="原会话不存在")
    ts = now_str()
    conn.execute(
        "UPDATE timeout_append_ledger SET replayed=1, replayed_at=?, note=? WHERE id=?",
        (ts, f"{row['note']}；已重放", ledger_id),
    )
    audit_id = _write_audit(
        conn, session_id=se["id"], slip_no=se["slip_no"], delta_kwh=row["delta_kwh"],
        power_kw=row["power_kw"], disposition=DISPOSITION_REPLAY, ledger_id=ledger_id,
        replayed_of=row["pulse_audit_id"], note="附加账脉冲重放，原会话状态与电量不变",
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (se["id"], "附加账重放", ts, f"{row['ledger_no']} 重放，原会话保持{se['status']}"),
    )
    return {
        "附加账编号": ledger_id, "附加账号": row["ledger_no"], "承接": DISPOSITION_REPLAY,
        "原会话状态": se["status"], "原会话累计电量": float(se["kwh"]), "审计编号": audit_id,
    }


def settle(conn, session_id: int) -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可关账")
    ts = now_str()
    price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
    amount = round(float(se["kwh"]) * price, 2)
    conn.execute(
        "UPDATE sessions SET status='已关账', amount=?, closed_at=?, price_locked=? WHERE id=?",
        (amount, ts, price, session_id),
    )
    conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (se["pile_id"],))
    conn.execute("UPDATE guns SET status='可用' WHERE id=?", (se["gun_id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "结算关账", ts, f"金额{amount}"),
    )
    return {"会话编号": session_id, "电量": float(se["kwh"]), "金额": amount, "状态": "已关账"}


def mark_timeout(conn, session_id: int) -> dict:
    # IMMEDIATE 行锁：与脉冲写口互斥，锁内重读状态，防止把已并入的脉冲拧花
    conn.execute("SELECT 1 FROM sessions WHERE id=? LIMIT 1", (session_id,))
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可标超时")
    minutes = int(get_setting(conn, "超时分钟", "120"))
    ts = now_str()
    # 只在仍为充电中时落超时，杜绝并发下把状态改回/覆盖
    cur = conn.execute(
        "UPDATE sessions SET status='已超时', note=? WHERE id=? AND status='充电中'",
        (f"超过{minutes}分钟未关", session_id),
    )
    if cur.rowcount != 1:
        raise HTTPException(409, detail="并发冲突：会话状态已变化，超时标未生效")
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "超时", ts, f"阈值{minutes}分钟；累计电量定格{se['kwh']}"),
    )
    return {"会话编号": session_id, "状态": "已超时", "定格电量": float(se["kwh"])}


def abort_session(conn, session_id: int, reason: str = "急停") -> dict:
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] not in ("充电中", "结算中", "已超时"):
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可中断")
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET status='已中断', closed_at=?, note=? WHERE id=?",
        (ts, reason, session_id),
    )
    conn.execute("UPDATE piles SET status='空闲' WHERE id=?", (se["pile_id"],))
    conn.execute("UPDATE guns SET status='可用' WHERE id=?", (se["gun_id"],))
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "异常中断", ts, reason),
    )
    return {"会话编号": session_id, "状态": "已中断", "原因": reason}


def set_pile_status(conn, pile_id: int, status: str) -> dict:
    if status not in ("空闲", "占用", "故障", "维护"):
        raise HTTPException(400, detail="非法桩位状态")
    pile = conn.execute("SELECT * FROM piles WHERE id=?", (pile_id,)).fetchone()
    if not pile:
        raise HTTPException(404, detail="桩位不存在")
    open_se = conn.execute(
        "SELECT id FROM sessions WHERE pile_id=? AND status IN ('充电中','结算中')",
        (pile_id,),
    ).fetchone()
    if open_se and status in ("故障", "维护"):
        # 底座默认：置故障/维护时若仍有在充，拒绝（后续题可改互斥）
        raise HTTPException(400, detail="仍有在充会话，不可直接改故障或维护")
    if status == "空闲" and open_se:
        raise HTTPException(400, detail="仍有在充会话，不可改空闲")
    conn.execute("UPDATE piles SET status=? WHERE id=?", (status, pile_id))
    return {"桩位编号": pile_id, "状态": status}
