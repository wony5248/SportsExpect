import type { Backtest, Game, OperationsStatus } from '../types'

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`
}

export async function fetchGames(date: string, league = 'ALL'): Promise<Game[]> {
  const response = await fetch(apiUrl(`/api/v1/games?date=${encodeURIComponent(date)}&league=${encodeURIComponent(league)}`))
  if (!response.ok) throw new Error(`API ${response.status}: 경기 데이터를 불러오지 못했습니다.`)
  const payload = await response.json() as { games: Game[] }
  return payload.games
}

export async function fetchOperations(): Promise<OperationsStatus> {
  const response = await fetch(apiUrl('/api/v1/operations/status'))
  if (!response.ok) throw new Error(`API ${response.status}: 운영 상태를 불러오지 못했습니다.`)
  return response.json() as Promise<OperationsStatus>
}

export async function fetchBacktest(league = 'ALL'): Promise<Backtest> {
  const response = await fetch(apiUrl(`/api/v1/model/backtest?league=${encodeURIComponent(league)}`))
  if (!response.ok) throw new Error(`API ${response.status}: 모델 평가를 불러오지 못했습니다.`)
  return response.json() as Promise<Backtest>
}

export function kstToday(): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date())
}
