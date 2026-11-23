from __future__ import annotations
from fastapi import HTTPException

from app.services.core import current_band_price, get_setting, is_day_sealed, now_str, today


def _next_slip(conn) -> str:
    n = int(get_setting(conn, "会话序号", "1000")) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('会话序号',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(n),),
    )
    return f"充{n}"


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


def add_pulse(conn, session_id: int, delta_kwh: float, power_kw: float = 0) -> dict:
    if delta_kwh <= 0:
        raise HTTPException(400, detail="脉冲电量须大于零")
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可记脉冲")
    ts = now_str()
    conn.execute(
        "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at) VALUES(?,?,?,?)",
        (session_id, delta_kwh, power_kw, ts),
    )
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
    return {"会话编号": session_id, "累计电量": round(new_kwh, 3), "预估金额": amount}


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
    se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise HTTPException(404, detail="会话不存在")
    if se["status"] != "充电中":
        raise HTTPException(400, detail=f"会话状态为{se['status']}，不可标超时")
    minutes = int(get_setting(conn, "超时分钟", "120"))
    ts = now_str()
    conn.execute(
        "UPDATE sessions SET status='已超时', note=? WHERE id=?",
        (f"超过{minutes}分钟未关", session_id),
    )
    conn.execute(
        "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
        (session_id, "超时", ts, f"阈值{minutes}分钟"),
    )
    return {"会话编号": session_id, "状态": "已超时"}


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
