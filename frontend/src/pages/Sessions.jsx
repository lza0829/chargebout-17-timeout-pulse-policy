import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Sessions() {
  const [list, setList] = useState([])
  const [detail, setDetail] = useState(null)
  useEffect(() => {
    api.会话().then(setList)
  }, [])
  return (
    <div className="page">
      <h2>会话册</h2>
      <p className="lead">最近会话列表；点开可看脉冲与事件流水。</p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>会话号</th>
              <th>站点桩位</th>
              <th>状态</th>
              <th>电量</th>
              <th>金额</th>
              <th>营业日</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td>{s.slip_no}</td>
                <td>
                  {s.site_name} · {s.pile_name} · {s.gun_name}
                </td>
                <td>
                  <span className={`tag ${s.status}`}>{s.status}</span>
                </td>
                <td>{s.kwh}</td>
                <td>{s.amount}</td>
                <td>{s.business_day}</td>
                <td>
                  <button type="button" onClick={() => api.会话详情(s.id).then(setDetail)}>
                    详情
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {detail && (
        <div className="panel">
          <h3>
            {detail.slip_no} · <span className={`tag ${detail.status}`}>{detail.status}</span>
          </h3>
          <p>
            锁定电价 {detail.price_locked} · 电量 {detail.kwh} · 金额 {detail.amount}
          </p>
          <h3>脉冲（仅在充期并入原会话）</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>电量增量</th>
                <th>功率</th>
              </tr>
            </thead>
            <tbody>
              {(detail.脉冲 || []).map((p) => (
                <tr key={p.id}>
                  <td>{p.created_at}</td>
                  <td>{p.delta_kwh}</td>
                  <td>{p.power_kw}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>超时附加账（超时后脉冲承接，不增原会话电量）</h3>
          <table>
            <thead>
              <tr>
                <th>附加账号</th>
                <th>电量</th>
                <th>金额</th>
                <th>审计关联</th>
                <th>重放</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              {(detail.超时附加账 || []).map((x) => (
                <tr key={x.id}>
                  <td>{x.ledger_no}</td>
                  <td>{x.delta_kwh}</td>
                  <td>{x.amount}</td>
                  <td>{x.pulse_audit_id ? `审计#${x.pulse_audit_id}` : '缺失'}</td>
                  <td>{x.replayed ? `已重放 ${x.replayed_at || ''}` : '待承接'}</td>
                  <td>{x.created_at}</td>
                </tr>
              ))}
              {!(detail.超时附加账 || []).length && (
                <tr><td colSpan={6} className="msg">无附加账</td></tr>
              )}
            </tbody>
          </table>
          <h3>脉冲审计（每个脉冲写口一笔，命运可重放）</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>命运</th>
                <th>电量</th>
                <th>附加账关联</th>
                <th>重放自</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.脉冲审计 || []).map((a) => (
                <tr key={a.id}>
                  <td>{a.created_at}</td>
                  <td><span className={`tag ${a.disposition}`}>{a.disposition}</span></td>
                  <td>{a.delta_kwh}</td>
                  <td>{a.ledger_id ? `附加账#${a.ledger_id}` : '—'}</td>
                  <td>{a.replayed_of ? `审计#${a.replayed_of}` : '—'}</td>
                  <td>{a.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h3>事件</h3>
          <table>
            <thead>
              <tr>
                <th>时间</th>
                <th>动作</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(detail.事件 || []).map((e) => (
                <tr key={e.id}>
                  <td>{e.created_at}</td>
                  <td>{e.action}</td>
                  <td>{e.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
