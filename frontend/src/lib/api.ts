import type { Backtest, ClaudeKeyStatus, ClaudeModel, Game, OperationsStatus } from '../types'

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

async function adminRequest(path: string, adminToken: string, init?: RequestInit) {
  const response = await fetch(apiUrl(path), {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      'X-Admin-Token': adminToken,
      ...init?.headers,
    },
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: string }
    throw new Error(payload.detail ?? `API ${response.status}: 관리자 요청에 실패했습니다.`)
  }
  return response
}

export async function fetchClaudeKeyStatus(adminToken: string): Promise<ClaudeKeyStatus> {
  const response = await adminRequest('/api/v1/admin/claude-key', adminToken)
  return response.json() as Promise<ClaudeKeyStatus>
}

export async function fetchClaudeModels(adminToken: string, apiKey?: string): Promise<ClaudeModel[]> {
  const response = await adminRequest('/api/v1/admin/claude-key/models', adminToken, {
    method: 'POST', body: JSON.stringify({ api_key: apiKey || null }),
  })
  const payload = await response.json() as { models: ClaudeModel[] }
  return payload.models
}

export async function registerClaudeKey(
  adminToken: string,
  apiKey: string | null,
  model: string,
  enabled: boolean,
): Promise<ClaudeKeyStatus> {
  const response = await adminRequest('/api/v1/admin/claude-key', adminToken, {
    method: 'POST', body: JSON.stringify({ api_key: apiKey || null, model, enabled }),
  })
  return response.json() as Promise<ClaudeKeyStatus>
}

export async function removeClaudeKey(adminToken: string): Promise<ClaudeKeyStatus> {
  const response = await adminRequest('/api/v1/admin/claude-key/remove', adminToken, { method: 'POST' })
  return response.json() as Promise<ClaudeKeyStatus>
}

export function kstToday(): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date())
}
