import os
import pathlib
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest

# 必须在导入 app 之前指定隔离库
_TMP = tempfile.mkdtemp(prefix="chargebout-test-")
os.environ["CHARGEBOUT_DB"] = str(pathlib.Path(_TMP) / "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import DB_PATH  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402


@pytest.fixture
def client():
    """每个用例一个全新种子库，定位面互不污染。"""
    for suffix in ("", "-wal", "-shm"):
        pathlib.Path(str(DB_PATH) + suffix).unlink(missing_ok=True)
    seed_if_empty()
    with TestClient(app) as c:
        yield c


def _open_session(client) -> int:
    piles = client.get("/api/桩位").json()
    pid = next(p["id"] for p in piles if p["status"] == "空闲")
    r = client.post("/api/开会话", json={"桩位编号": pid})
    assert r.status_code == 200, r.text
    return r.json()["会话编号"]


def _audits(client, sid):
    return client.get(f"/api/脉冲审计?会话编号={sid}").json()


def _ledger(client, sid):
    return client.get(f"/api/超时附加账?会话编号={sid}").json()


def test_超时后脉冲落附加账且原会话电量不增状态不改回(client):
    """主口径：已超时 → 脉冲进附加账；原会话保持已超时、累计电量定格。"""
    sid = _open_session(client)
    client.post(f"/api/会话/{sid}/脉冲", json={"电量": 2.0, "功率": 7})
    client.post(f"/api/会话/{sid}/超时")
    before = client.get(f"/api/会话/{sid}").json()
    assert before["status"] == "已超时"

    for kwh in (3.0, 1.5, 0.5):
        r = client.post(
            f"/api/会话/{sid}/脉冲",
            json={"电量": kwh, "功率": 6, "幂等令牌": f"tok-{kwh}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["承接"] == "附加账承接"
        assert body["原会话累计电量"] == 2.0
        # 每一笔之后原会话都不得被改回充电中、不得偷增电量
        cur = client.get(f"/api/会话/{sid}").json()
        assert cur["status"] == "已超时"
        assert cur["kwh"] == 2.0

    led = _ledger(client, sid)
    assert [x["delta_kwh"] for x in led] == [0.5, 1.5, 3.0]  # 倒序
    assert sum(x["delta_kwh"] for x in led) == 5.0


def test_错误承接臂守卫_超时脉冲绝不并入原会话(client):
    """反向夹具：防止实现滑回臂A——超时后脉冲绝不能写 pulses 表、绝不能增 kwh。"""
    sid = _open_session(client)
    client.post(f"/api/会话/{sid}/脉冲", json={"电量": 2.0})
    client.post(f"/api/会话/{sid}/超时")
    client.post(f"/api/会话/{sid}/脉冲", json={"电量": 4.0, "幂等令牌": "x"})

    det = client.get(f"/api/会话/{sid}").json()
    merged_deltas = [p["delta_kwh"] for p in det["脉冲"]]
    assert merged_deltas == [2.0]  # 超时后的 4.0 绝不在原会话脉冲流水
    assert det["kwh"] == 2.0
    assert det["status"] == "已超时"


def test_附加账与审计双向关联不得丢(client):
    """附加账有行必有审计关联；附加账承接审计必回指附加账。"""
    sid = _open_session(client)
    client.post(f"/api/会话/{sid}/脉冲", json={"电量": 1.0})
    client.post(f"/api/会话/{sid}/超时")
    for i in range(3):
        client.post(
            f"/api/会话/{sid}/脉冲",
            json={"电量": 1.0 + i, "幂等令牌": f"t{i}"},
        )

    led = _ledger(client, sid)
    audits = {a["id"]: a for a in _audits(client, sid)}
    assert len(led) == 3
    for row in led:
        assert row["session_id"] == sid
        assert row["slip_no"]  # 关联字段：原会话号
        assert row["pulse_audit_id"] is not None, "附加账无审计关联即未闭合"
        a = audits[row["pulse_audit_id"]]
        assert a["disposition"] == "附加账承接"
        assert a["ledger_id"] == row["id"]  # 审计反向回指
        assert a["session_id"] == sid

    ledger_audits = [a for a in audits.values() if a["disposition"] == "附加账承接"]
    assert len(ledger_audits) == len(led)


def test_超时标与脉冲并发半截_回滚后不偷增不改回(client):
    """并发压测：超时标与多脉冲同时打，只允许串行后的确定命运，不允许拧花。"""
    sid = _open_session(client)

    def pulse(i):
        return client.post(
            f"/api/会话/{sid}/脉冲",
            json={"电量": 1.0, "功率": 7, "幂等令牌": f"c{i}"},
        )

    def timeout_marker():
        return client.post(f"/api/会话/{sid}/超时")

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(pulse, i) for i in range(10)]
        futs.append(pool.submit(timeout_marker))
        results = [f.result() for f in futs]

    for r in results:
        assert r.status_code == 200, r.text  # 不允许 database locked / 500

    det = client.get(f"/api/会话/{sid}").json()
    audits = _audits(client, sid)
    led = _ledger(client, sid)
    merged = [a for a in audits if a["disposition"] == "并入原会话"]
    appended = [a for a in audits if a["disposition"] == "附加账承接"]

    # 命运总数闭合：10 个脉冲每个恰好一条审计，不多不少
    assert len(merged) + len(appended) == 10
    assert det["status"] in ("充电中", "已超时")

    # 电量严格等于已并入脉冲之和，附加账电量绝不偷增进原会话
    merged_sum = round(sum(p["delta_kwh"] for p in det["脉冲"]), 3)
    assert det["kwh"] == merged_sum
    assert len(det["脉冲"]) == len(merged)
    if appended:
        assert det["kwh"] < merged_sum + sum(x["delta_kwh"] for x in led)

    # 顺序口径：先并入后附加，绝不允许出现附加之后又并入（状态被改回充电中）
    timeline = [a["disposition"] for a in sorted(audits, key=lambda a: a["id"])]
    seen_append = False
    for d in timeline:
        if d == "附加账承接":
            seen_append = True
        assert not (seen_append and d == "并入原会话"), "超时后出现并入，状态被拧回充电中"
    if appended:
        assert det["status"] == "已超时"

    # 附加账↔审计关联在并发下依然完整
    audit_ids = {a["id"] for a in audits}
    for row in led:
        assert row["pulse_audit_id"] in audit_ids


def test_幂等令牌重放不重复落账(client):
    sid = _open_session(client)
    client.post(f"/api/会话/{sid}/超时")
    payload = {"电量": 2.5, "功率": 6, "幂等令牌": "once"}
    r1 = client.post(f"/api/会话/{sid}/脉冲", json=payload).json()
    r2 = client.post(f"/api/会话/{sid}/脉冲", json=payload).json()
    assert r2.get("幂等重放") is True
    assert r1["附加账编号"] == r2["附加账编号"]
    assert len(_ledger(client, sid)) == 1
    assert len([a for a in _audits(client, sid) if a["disposition"] == "附加账承接"]) == 1


def test_附加账重放不改原会话状态与电量(client):
    sid = _open_session(client)
    client.post(f"/api/会话/{sid}/脉冲", json={"电量": 2.0})
    client.post(f"/api/会话/{sid}/超时")
    p = client.post(
        f"/api/会话/{sid}/脉冲", json={"电量": 3.0, "幂等令牌": "r"}
    ).json()
    rep = client.post(f"/api/超时附加账/{p['附加账编号']}/重放")
    assert rep.status_code == 200
    body = rep.json()
    assert body["承接"] == "重放承接"
    assert body["原会话状态"] == "已超时"
    assert body["原会话累计电量"] == 2.0

    row = next(x for x in _ledger(client, sid) if x["id"] == p["附加账编号"])
    assert row["replayed"] == 1 and row["replayed_at"]
    det = client.get(f"/api/会话/{sid}").json()
    assert det["status"] == "已超时" and det["kwh"] == 2.0
    assert any(a["disposition"] == "重放承接" for a in det["脉冲审计"])


def test_关账后脉冲拒收但审计必须落盘(client):
    """回归：拒收审计曾随 HTTPException 被回滚，导致脉冲丢弃无审计。"""
    sid = _open_session(client)
    client.post(f"/api/会话/{sid}/脉冲", json={"电量": 2.0})
    client.post(f"/api/会话/{sid}/结算")
    r = client.post(f"/api/会话/{sid}/脉冲", json={"电量": 1.0})
    assert r.status_code == 400
    det = client.get(f"/api/会话/{sid}").json()
    assert det["kwh"] == 2.0  # 拒收不增电量
    rejected = [a for a in det["脉冲审计"] if a["disposition"] == "拒收"]
    assert len(rejected) == 1 and rejected[0]["http_status"] == 400


def test_脉冲写口中途异常整体回滚不偷增(client, monkeypatch):
    """半截故障：事务内抛错必须整体回滚，脉冲/审计/累计一个都不能留半笔。"""
    sid = _open_session(client)
    import app.services.sessions as svc

    def boom(*_a, **_kw):
        raise RuntimeError("模拟写口半截故障")

    # 在审计写入处炸掉：此时脉冲行与累计更新已在同一事务内，须一并回滚
    monkeypatch.setattr(svc, "_write_audit", boom)
    with pytest.raises(Exception):
        client.post(f"/api/会话/{sid}/脉冲", json={"电量": 2.0})

    det = client.get(f"/api/会话/{sid}").json()
    assert det["kwh"] == 0
    assert det["脉冲"] == []
    assert det["脉冲审计"] == []
    assert [e["action"] for e in det["事件"]] == ["开会话"]


def test_总览超时桶与超时页会话册附加账同口径(client):
    """总览超时桶、超时页（已超时列表）、附加账流水三方必须对得上。"""
    s1, s2 = _open_session(client), _open_session(client)
    for sid in (s1, s2):
        client.post(f"/api/会话/{sid}/脉冲", json={"电量": 2.0})
        client.post(f"/api/会话/{sid}/超时")
        client.post(f"/api/会话/{sid}/脉冲", json={"电量": 3.0, "幂等令牌": f"o{sid}"})

    ov = client.get("/api/总览").json()
    timeout_page = client.get("/api/会话?状态=已超时").json()
    led_all = client.get("/api/超时附加账").json()

    assert ov["已超时"] == ov["超时桶会话数"] == len(timeout_page) == 2
    assert ov["超时附加账笔数"] == len(led_all) == 2
    assert ov["超时附加电量"] == round(sum(x["delta_kwh"] for x in led_all), 3) == 6.0
    assert ov["超时附加金额"] == round(sum(x["amount"] for x in led_all), 2)
    # 附加电量绝不混入在充电量
    assert ov["在充电量"] == 0.0
    # 会话册详情里同样能摸到附加账与审计（同刷）
    for sid in (s1, s2):
        det = client.get(f"/api/会话/{sid}").json()
        assert len(det["超时附加账"]) == 1
        assert any(a["disposition"] == "附加账承接" for a in det["脉冲审计"])


def test_重复超时标被拒且不覆盖定格状态(client):
    sid = _open_session(client)
    client.post(f"/api/会话/{sid}/脉冲", json={"电量": 2.0})
    client.post(f"/api/会话/{sid}/超时")
    r = client.post(f"/api/会话/{sid}/超时")
    assert r.status_code == 400
    det = client.get(f"/api/会话/{sid}").json()
    assert det["status"] == "已超时" and det["kwh"] == 2.0
