import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Pulses() {
  const [list, setList] = useState([])
  const [id, setId] = useState('')
  const [kwh, setKwh] = useState('1.5')
  const [kw, setKw] = useState('7')
  const [token, setToken] = useState('')
  const [msg, setMsg] = useState('')
  const [ok, setOk] = useState(false)

  const load = () =>
    Promise.all([api.会话('充电中'), api.会话('已超时')]).then(([a, b]) => {
      const rows = [...a, ...b]
      setList(rows)
      if (!rows.some((s) => String(s.id) === id) && rows[0]) setId(String(rows[0].id))
    })

  useEffect(() => {
    load()
  }, [])

  const submit = async () => {
    setMsg('')
    setOk(false)
    try {
      const r = await api.脉冲(Number(id), {
        电量: Number(kwh),
        功率: Number(kw),
        幂等令牌: token || null,
      })
      if (r.承接 === '附加账承接') {
        setMsg(
          `会话已超时 → 落附加账 ${r.附加账号}：附加电量 ${r.附加电量}，` +
            `原会话累计不变 ${r.原会话累计电量}（审计#${r.审计编号}）`,
        )
      } else {
        setMsg(`累计电量 ${r.累计电量}，预估金额 ${r.预估金额}（审计#${r.审计编号}）`)
      }
      setOk(true)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }

  return (
    <div className="page">
      <h2>记脉冲</h2>
      <p className="lead">
        充电中脉冲并入原会话；已超时脉冲整笔落超时附加账（不增原会话电量、不改回充电中）；其它状态拒收并记审计。
      </p>
      <div className="panel">
        <div className="row">
          <label>会话</label>
          <select value={id} onChange={(e) => setId(e.target.value)}>
            {list.map((s) => (
              <option key={s.id} value={s.id}>
                {s.slip_no} · {s.pile_name} · {s.status} · 已充 {s.kwh}
              </option>
            ))}
          </select>
          <label>电量</label>
          <input value={kwh} onChange={(e) => setKwh(e.target.value)} style={{ width: 90 }} />
          <label>功率</label>
          <input value={kw} onChange={(e) => setKw(e.target.value)} style={{ width: 90 }} />
          <label>幂等令牌</label>
          <input value={token} onChange={(e) => setToken(e.target.value)} style={{ width: 140 }} placeholder="可选，重放防重" />
          <button type="button" className="primary" onClick={submit}>
            写入脉冲
          </button>
        </div>
        <p className={`msg ${ok ? 'ok' : ''}`}>{msg}</p>
      </div>
    </div>
  )
}
