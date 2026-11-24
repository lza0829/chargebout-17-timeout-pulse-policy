# 充桩条

开会话 → 计量脉冲累加 → 结算关账 → 超时/异常命运。

- 前端 http://127.0.0.1:6500
- API http://127.0.0.1:11500

```powershell
# 本地 API
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 11500

# 本地前端（另开）
cd frontend
npm install
npm run dev
```

或 `docker compose up -d --build`。

## 超时后脉冲承接（全局唯一口径：臂B 附加账承接）

会话已超时后到达的脉冲，全局只走一条路径，禁止与「并入原会话」混用：

- 原会话保持 `已超时`，`kwh/amount` 定格不增，**绝不改回充电中**。
- 脉冲整笔写入 `timeout_append_ledger`（附加账号 `会话号-附N`，`session_id/slip_no` 关联原会话）。
- 每个脉冲写口同步写一条 `pulse_audit`（命运：`并入原会话 / 附加账承接 / 重放承接 / 拒收`），附加账与审计双向回填关联。
- 已关账/已中断等状态的脉冲整笔拒收（400），但拒收审计先提交落盘，杜绝「脉冲丢弃无审计」。
- 附加账可重放（`/超时附加账/{id}/重放`），重放只留轨迹，不改原会话状态与电量；脉冲写口支持 `幂等令牌` 防重。
- 超时标与脉冲写口统一走 `BEGIN IMMEDIATE` 事务，SQLite 写锁串行化，并发半截即整体回滚，电量不偷增。
- 总览超时桶、超时页、会话册、脉冲审计同刷同口径；附加电量不计入「在充电量」。

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

