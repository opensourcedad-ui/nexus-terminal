import { createClient } from '@supabase/supabase-js'

export type PulseRow = {
  contract: string; label: string | null; category: string | null
  txs_24h: number; wallets_24h: number
  txs_prev_24h: number; wallets_prev_24h: number
  txs_7d: number; wallets_7d: number; updated_at: number
}
export type FreshRow = {
  contract: string; first_seen_block: number; first_seen_at: number
  created_onchain: boolean; txs_24h: number; wallets_24h: number; label: string | null
}
export type SignalRow = {
  contract: string; smart_wallets: number; last_hit_at: number; label: string | null
}

const url = import.meta.env.VITE_SUPABASE_URL as string | undefined
const key = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined
const supabase = url && key ? createClient(url, key) : null
export const isDemo = !supabase

const now = Math.floor(Date.now() / 1000)
const demoApps = [
  ['Roach Racing Club', 'game', 4120, 38900, 1180, 11400],
  ['Myriad Markets', 'prediction', 2890, 21300, 2640, 19800],
  ['Gacha Galaxy', 'game', 1975, 17750, 2310, 21100],
  ['Big Coin Flip', 'casino', 1410, 26200, 1455, 27100],
  ['Penguin Life', 'social', 980, 6900, 610, 4100],
  ['Top Hat', 'trading', 760, 9100, 790, 9600],
  ['Kabu', 'nft', 540, 3100, 720, 4400],
  ['Captain & Company', 'game', 410, 2800, 380, 2600],
] as const

export const demoPulse: PulseRow[] = demoApps.map(([label, category, w, t, pw, pt], i) => ({
  contract: `0x${(i + 1).toString(16).padStart(40, 'a')}`,
  label, category,
  wallets_24h: w, txs_24h: t, wallets_prev_24h: pw, txs_prev_24h: pt,
  wallets_7d: w * 6, txs_7d: t * 6, updated_at: now - 120,
}))

export const demoFresh: FreshRow[] = [
  { contract: '0xf1e2d3c4b5a69788f1e2d3c4b5a69788f1e2d3c4', first_seen_block: 9412882, first_seen_at: now - 3600 * 2, created_onchain: true, txs_24h: 312, wallets_24h: 187, label: null },
  { contract: '0xbeef00aa11bb22cc33dd44ee55ff667788990011', first_seen_block: 9408104, first_seen_at: now - 3600 * 9, created_onchain: true, txs_24h: 96, wallets_24h: 41, label: null },
  { contract: '0x0042aa42bb42cc42dd42ee42ff42004211421142', first_seen_block: 9391773, first_seen_at: now - 3600 * 26, created_onchain: false, txs_24h: 1204, wallets_24h: 530, label: 'unverified mint' },
]

export const demoSignals: SignalRow[] = [
  { contract: '0xf1e2d3c4b5a69788f1e2d3c4b5a69788f1e2d3c4', smart_wallets: 47, last_hit_at: now - 1800, label: null },
  { contract: '0x0042aa42bb42cc42dd42ee42ff42004211421142', smart_wallets: 23, last_hit_at: now - 5400, label: 'unverified mint' },
  { contract: '0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1', smart_wallets: 12, last_hit_at: now - 9000, label: 'Roach Racing Club' },
]

async function pull<T>(table: string, order: string, fallback: T[]): Promise<T[]> {
  if (!supabase) return fallback
  const { data, error } = await supabase.from(table).select('*').order(order, { ascending: false }).limit(200)
  if (error || !data || data.length === 0) return fallback
  return data as T[]
}

export const fetchPulse = () => pull<PulseRow>('app_pulse', 'wallets_24h', demoPulse)
export const fetchFresh = () => pull<FreshRow>('fresh_contracts', 'first_seen_at', demoFresh)
export const fetchSignals = () => pull<SignalRow>('smart_signals', 'smart_wallets', demoSignals)
