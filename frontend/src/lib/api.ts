import type { Backtest, ClaudeKeyStatus, ClaudeModel, Game, GameDate, OperationsStatus } from '../types'

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`
}

async function request(path: string, init?: RequestInit, timeoutMs = 20_000): Promise<Response> {
  const canRetry = !init?.method || init.method.toUpperCase() === 'GET'
  const attempts = canRetry ? 3 : 1
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), timeoutMs)
    try {
      const response = await fetch(apiUrl(path), { ...init, signal: controller.signal })
      if (attempt < attempts - 1 && [502, 503, 504].includes(response.status)) {
        await new Promise((resolve) => window.setTimeout(resolve, (attempt + 1) * 1_000))
        continue
      }
      return response
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') {
        throw new Error('서버 응답 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.')
      }
      if (attempt < attempts - 1 && canRetry) {
        await new Promise((resolve) => window.setTimeout(resolve, (attempt + 1) * 1_000))
        continue
      }
      throw new Error('서버에 연결할 수 없습니다. 네트워크 상태와 API 배포 상태를 확인해 주세요.')
    } finally {
      window.clearTimeout(timer)
    }
  }
  throw new Error('서버에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.')
}

export async function fetchGames(date: string, league = 'ALL'): Promise<Game[]> {
  const response = await request(`/api/v1/games?date=${encodeURIComponent(date)}&league=${encodeURIComponent(league)}`)
  if (!response.ok) throw new Error(`API ${response.status}: 경기 데이터를 불러오지 못했습니다.`)
  const payload = await response.json() as { games: Game[] }
  if (!Array.isArray(payload.games)) throw new Error('경기 데이터 형식이 올바르지 않습니다.')
  return payload.games
}

export async function fetchGameDates(year: number, league = 'ALL'): Promise<GameDate[]> {
  const response = await request(`/api/v1/game-dates?year=${year}&league=${encodeURIComponent(league)}`)
  if (!response.ok) throw new Error(`API ${response.status}: 시즌 경기일을 불러오지 못했습니다.`)
  const payload = await response.json() as { dates: GameDate[] }
  return Array.isArray(payload.dates) ? payload.dates : []
}

export async function fetchOperations(): Promise<OperationsStatus> {
  const response = await request('/api/v1/operations/status', undefined, 12_000)
  if (!response.ok) throw new Error(`API ${response.status}: 운영 상태를 불러오지 못했습니다.`)
  return response.json() as Promise<OperationsStatus>
}

export async function fetchBacktest(league = 'ALL'): Promise<Backtest> {
  const response = await request(`/api/v1/model/backtest?league=${encodeURIComponent(league)}`, undefined, 12_000)
  if (!response.ok) throw new Error(`API ${response.status}: 모델 평가를 불러오지 못했습니다.`)
  return response.json() as Promise<Backtest>
}

async function adminRequest(path: string, adminToken: string, init?: RequestInit) {
  const response = await request(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      'X-Admin-Token': adminToken,
      ...init?.headers,
    },
  }, 25_000)
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
