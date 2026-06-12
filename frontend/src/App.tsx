import { useEffect, useMemo, useState } from 'react'
import {
  fetchPulse, fetchFresh, fetchSignals, isDemo,
  type PulseRow, type FreshRow, type SignalRow,
} from './lib/data'

type Tab = 'pulse' | 'fresh' | 'signals'

const short = (a: string) => `${a.slice(0, 6)}…${a.slice(-4)}`
const fmt = (n: number) => n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n)
const fmtUsd = (n: number | null) => {
  const v = n ?? 0
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}m`
  if (v >= 1_000) return `$${(v / 1_000).toFixed(v >= 10_000 ? 0 : 1)}k`
  return `$${v.toFixed(0)}`
}
const ago = (ts: number) => {
  const s = Math.max(1, Math.floor(Date.now() / 1000) - ts)
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

function Delta({ cur, prev }: { cur: number; prev: number }) {
  if (prev === 0) return <span className="delta new">new</span>
  const pct = ((cur - prev) / prev) * 100
  const cls = pct >= 0 ? 'up' : 'down'
  return <span className={`delta ${cls}`}>{pct >= 0 ? '▲' : '▼'} {Math.abs(pct).toFixed(0)}%</span>
}

function Bar({ value, max }: { value: number; max: number }) {
  return (
    <div className="bar"><div className="bar-fill" style={{ width: `${Math.max(2, (value / max) * 100)}%` }} /></div>
  )
}

type Rank = 'wallets' | 'volume'

export default function App() {
  const [tab, setTab] = useState<Tab>('pulse')
  const [rank, setRank] = useState<Rank>('wallets')
  const [pulse, setPulse] = useState<PulseRow[]>([])
  const [fresh, setFresh] = useState<FreshRow[]>([])
  const [signals, setSignals] = useState<SignalRow[]>([])
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let live = true
    const load = async () => {
      const [p, f, s] = await Promise.all([fetchPulse(), fetchFresh(), fetchSignals()])
      if (!live) return
      setPulse(p); setFresh(f); setSignals(s); setLoaded(true)
    }
    load()
    const id = setInterval(load, 60_000)
    return () => { live = false; clearInterval(id) }
  }, [])

  const ranked = useMemo(() => {
    const metric = (p: PulseRow) => rank === 'volume' ? (p.vol_usd_24h ?? 0) : p.wallets_24h
    return { rows: [...pulse].sort((a, b) => metric(b) - metric(a)), metric }
  }, [pulse, rank])
  const maxMetric = useMemo(
    () => Math.max(1, ...ranked.rows.map(ranked.metric)), [ranked])
  const tickerItems = useMemo(() => {
    const items: string[] = []
    fresh.slice(0, 6).forEach(f =>
      items.push(`◉ ${f.created_onchain ? 'DEPLOYED' : 'FIRST SEEN'} ${short(f.contract)} · ${f.wallets_24h} wallets`))
    signals.slice(0, 4).forEach(s =>
      items.push(`★ ${s.smart_wallets} tracked wallets → ${s.label ?? short(s.contract)}`))
    return items.length ? items : ['◉ awaiting first indexed block']
  }, [fresh, signals])

  return (
    <div className="shell">
      <div className="ticker" aria-hidden="true">
        <div className="ticker-track">
          {[...tickerItems, ...tickerItems].map((t, i) => <span key={i}>{t}</span>)}
        </div>
      </div>

      <header>
        <div className="brand">
          <h1>NEXUS<span>TERMINAL</span></h1>
          <p className="tag">the live activity layer for Abstract</p>
        </div>
        <div className={`status ${isDemo ? 'demo' : 'live'}`}>
          <i />{isDemo ? 'demo data' : 'live · refreshes 60s'}
        </div>
      </header>

      <nav>
        <button className={tab === 'pulse' ? 'on' : ''} onClick={() => setTab('pulse')}>App Pulse</button>
        <button className={tab === 'fresh' ? 'on' : ''} onClick={() => setTab('fresh')}>Fresh Deployments</button>
        <button className={tab === 'signals' ? 'on' : ''} onClick={() => setTab('signals')}>Smart Signals</button>
      </nav>

      {!loaded && <p className="empty">connecting…</p>}

      {loaded && tab === 'pulse' && (
        <section>
          <div className="lede-row">
            <p className="lede">
              {rank === 'wallets'
                ? 'Every app ranked by real wallets in the last 24h. Onchain data only — votes can\'t touch this list.'
                : 'Every app ranked by USD that moved through it in the last 24h — ETH, PENGU and USDC.e flows, priced live.'}
            </p>
            <div className="rankbar" role="tablist" aria-label="rank by">
              <button className={rank === 'wallets' ? 'on' : ''} onClick={() => setRank('wallets')}>wallets</button>
              <button className={rank === 'volume' ? 'on' : ''} onClick={() => setRank('volume')}>volume</button>
            </div>
          </div>
          <div className="table">
            <div className="row head">
              <span>#</span><span>app</span><span className="num">wallets 24h</span>
              <span className="num">vol 24h</span><span className="num">Δ vs prev 24h</span>
              <span className="num">txs 24h</span><span className="grow">share</span>
            </div>
            {ranked.rows.map((p, i) => (
              <div className="row" key={p.contract}>
                <span className="rank">{i + 1}</span>
                <span className="name">
                  {p.label ?? <code>{short(p.contract)}</code>}
                  {p.category && <em>{p.category}</em>}
                </span>
                <span className={`num ${rank === 'wallets' ? 'strong' : 'dim'}`}>{fmt(p.wallets_24h)}</span>
                <span className={`num ${rank === 'volume' ? 'strong' : 'dim'}`}>{fmtUsd(p.vol_usd_24h)}</span>
                <span className="num">{rank === 'volume'
                  ? <Delta cur={p.vol_usd_24h ?? 0} prev={p.vol_usd_prev_24h ?? 0} />
                  : <Delta cur={p.wallets_24h} prev={p.wallets_prev_24h} />}</span>
                <span className="num dim">{fmt(p.txs_24h)}</span>
                <span className="grow"><Bar value={ranked.metric(p)} max={maxMetric} /></span>
              </div>
            ))}
          </div>
        </section>
      )}

      {loaded && tab === 'fresh' && (
        <section>
          <p className="lede">New contracts the moment they get traction — before any portal listing.</p>
          <div className="table">
            <div className="row head fresh-grid">
              <span>contract</span><span>type</span><span className="num">first seen</span>
              <span className="num">wallets 24h</span><span className="num">txs 24h</span>
            </div>
            {fresh.map(f => (
              <div className="row fresh-grid" key={f.contract}>
                <span className="name"><code>{short(f.contract)}</code>{f.label && <em>{f.label}</em>}</span>
                <span>{f.created_onchain
                  ? <span className="pill deploy">deployed</span>
                  : <span className="pill seen">first seen</span>}</span>
                <span className="num dim">{ago(f.first_seen_at)}</span>
                <span className="num strong">{fmt(f.wallets_24h)}</span>
                <span className="num dim">{fmt(f.txs_24h)}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {loaded && tab === 'signals' && (
        <section>
          <p className="lede">Where tracked OG wallets — top badge holders, early minters — moved in the last 48h.</p>
          <div className="table">
            <div className="row head sig-grid">
              <span>contract</span><span className="num">tracked wallets in</span><span className="num">last hit</span>
            </div>
            {signals.map(s => (
              <div className="row sig-grid" key={s.contract}>
                <span className="name">{s.label ?? <code>{short(s.contract)}</code>}</span>
                <span className="num strong glow">{s.smart_wallets}</span>
                <span className="num dim">{ago(s.last_hit_at)}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      <footer>
        <span>data: Abstract mainnet RPC, indexed independently</span>
        <a href="https://twitter.com/intent/tweet?text=tracking%20real%20abstract%20app%20usage%20on%20nexusterminal.xyz" target="_blank" rel="noreferrer">share ↗</a>
      </footer>
    </div>
  )
}
