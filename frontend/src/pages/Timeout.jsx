import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Timeout() {
  const [open, setOpen] = useState([])
  const [timedOut, setTimedOut] = useState([])
  const [ledger, setLedger] = useState([])
  const [msg, setMsg] = useState('')

  const load = () =>
    Promise.all([
      api.会话('充电中'),
      api.会话('已超时'),
      api.超时附加账(),
    ]).then(([a, b, c]) => {
      setOpen(a)
      setTimedOut(b)
      setLedger(c)
    })

  useEffect(() => {
    load()
  }, [])

  const mark = async (id) => {
    try {
      const r = await api.超时(id)
      setMsg(`会话 ${r.会话编号} 已标 ${r.状态}，电量定格 ${r.定格电量}`)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  const abort = async (id) => {
    try {
      const r = await api.中断(id, '急停')
      setMsg(`会话 ${r.会话编号} ${r.状态}`)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  const replay = async (id) => {
    try {
      const r = await api.附加账重放(id)
      setMsg(`${r.附加账号} 已${r.承接}，原会话仍为 ${r.原会话状态}，电量 ${r.原会话累计电量}`)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  const bySession = (sid) => ledger.filter((x) => x.session_id === sid)

  return (
    <div className="page">
      <h2>超时与中断</h2>
      <p className="lead">
        对充电中会话标超时；超时后脉冲按臂B落入附加账，原会话保持已超时、电量不增，可重放、可审计。
      </p>

      <div className="panel">
        <h3>充电中会话（超时标写口）</h3>
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>桩位</th>
              <th>电量</th>
              <th>开始时间</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {open.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>{s.site_name} · {s.pile_name}</td>
                <td>{s.kwh}</td>
                <td>{s.started_at}</td>
                <td className="row" style={{ border: 0 }}>
                  <button type="button" onClick={() => mark(s.id)}>标超时</button>
                  <button type="button" className="danger" onClick={() => abort(s.id)}>急停中断</button>
                </td>
              </tr>
            ))}
            {!open.length && (
              <tr><td colSpan={5} className="msg">暂无充电中会话</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>已超时会话与附加账承接（超时页）</h3>
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>状态</th>
              <th>原会话电量（定格）</th>
              <th>附加账号</th>
              <th>附加电量</th>
              <th>附加金额</th>
              <th>审计关联</th>
              <th>重放</th>
            </tr>
          </thead>
          <tbody>
            {timedOut.map((s) => {
              const rows = bySession(s.id)
              if (!rows.length) {
                return (
                  <tr key={s.id}>
                    <td>{s.slip_no}</td>
                    <td><span className={`tag ${s.status}`}>{s.status}</span></td>
                    <td>{s.kwh}</td>
                    <td colSpan={4} className="msg">超时后暂无脉冲</td>
                  </tr>
                )
              }
              return rows.map((x, i) => (
                <tr key={x.id}>
                  {i === 0 && (
                    <>
                      <td rowSpan={rows.length}>{s.slip_no}</td>
                      <td rowSpan={rows.length}>
                        <span className={`tag ${s.status}`}>{s.status}</span>
                      </td>
                      <td rowSpan={rows.length}>{s.kwh}</td>
                    </>
                  )}
                  <td>{x.ledger_no}</td>
                  <td>{x.delta_kwh}</td>
                  <td>{x.amount}</td>
                  <td>{x.pulse_audit_id ? `审计#${x.pulse_audit_id}` : '缺失'}</td>
                  <td>
                    {x.replayed ? (
                      `已于 ${x.replayed_at} 重放`
                    ) : (
                      <button type="button" onClick={() => replay(x.id)}>重放</button>
                    )}
                  </td>
                </tr>
              ))
            })}
            {!timedOut.length && (
              <tr><td colSpan={8} className="msg">暂无已超时会话</td></tr>
            )}
          </tbody>
        </table>
        <p className="msg">{msg}</p>
      </div>
    </div>
  )
}
