import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Box, Button, CircularProgress, Container, Stack, TextField, ToggleButton, ToggleButtonGroup, Typography } from '@mui/material'
import RefreshRounded from '@mui/icons-material/RefreshRounded'
import SettingsRounded from '@mui/icons-material/SettingsRounded'
import SportsBaseballRounded from '@mui/icons-material/SportsBaseballRounded'
import LoginRounded from '@mui/icons-material/LoginRounded'
import LogoutRounded from '@mui/icons-material/LogoutRounded'
import { fetchBacktest, fetchGameDates, fetchGames, fetchOperations, kstToday } from './lib/api'
import { loadAuthSession, onAuthStateChange, signOut } from './lib/auth'
import type { AuthSession } from './lib/auth'
import type { Backtest, Game, GameDate, OperationsStatus } from './types'
import { useMobile } from './lib/useMobile'
import DatePicker from './components/DatePicker'
import GameCard from './components/GameCard'
import ClaudeSettingsDialog from './components/ClaudeSettingsDialog'
import LoginDialog from './components/LoginDialog'

export default function App() {
  const initialQuery = new URLSearchParams(window.location.search)
  const [date, setDate] = useState(initialQuery.get('date') ?? kstToday)
  const [games, setGames] = useState<Game[]>([])
  const initialLeague = initialQuery.get('league')
  const [league, setLeague] = useState<'ALL' | 'KBO' | 'MLB'>(initialLeague === 'KBO' || initialLeague === 'MLB' ? initialLeague : 'ALL')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [operations, setOperations] = useState<OperationsStatus | null>(null)
  const [backtest, setBacktest] = useState<Backtest | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [loginOpen, setLoginOpen] = useState(false)
  const [session, setSession] = useState<AuthSession | null>(null)
  const [seasonDates, setSeasonDates] = useState<GameDate[]>([])
  const mobile = useMobile()
  const requestId = useRef(0)
  const seasonYear = Number(date.slice(0, 4))
  const hasActiveGames = date === kstToday() && games.some((game) => game.status === 'SCHEDULED' || game.status === 'LIVE')

  const load = useCallback(async (background = false) => {
    const currentRequest = ++requestId.current
    if (!background) { setLoading(true); setError(null) }
    const auxiliary = background ? null : Promise.allSettled([fetchOperations(), fetchBacktest(league)])
    try {
      const gameRows = await fetchGames(date, league)
      if (currentRequest !== requestId.current) return
      setGames(gameRows)
    }
    catch (err) {
      if (currentRequest !== requestId.current) return
      if (!background) {
        setGames([])
        setError(err instanceof Error ? err.message : '알 수 없는 오류')
      }
    }
    finally {
      if (!background && currentRequest === requestId.current) setLoading(false)
    }

    if (!auxiliary) return
    const [operationResult, backtestResult] = await auxiliary
    if (currentRequest !== requestId.current) return
    if (operationResult.status === 'fulfilled') setOperations(operationResult.value)
    if (backtestResult.status === 'fulfilled') setBacktest(backtestResult.value)
  }, [date, league])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    if (!hasActiveGames) return
    const timer = window.setInterval(() => { void load(true) }, 60_000)
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') void load(true)
    }
    document.addEventListener('visibilitychange', refreshWhenVisible)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', refreshWhenVisible)
    }
  }, [hasActiveGames, load])
  useEffect(() => {
    let active = true
    void loadAuthSession().then((next) => { if (active) setSession(next) }).catch(() => { if (active) setSession(null) })
    const unsubscribe = onAuthStateChange((next) => { if (active) setSession(next) })
    return () => { active = false; unsubscribe() }
  }, [])
  useEffect(() => {
    void fetchGameDates(seasonYear, league).then(setSeasonDates).catch(() => setSeasonDates([]))
  }, [seasonYear, league])
  useEffect(() => {
    const query = new URLSearchParams({ date, league })
    window.history.replaceState(null, '', `${window.location.pathname}?${query}`)
  }, [date, league])

  const openClaudeSettings = () => {
    if (!session) { setLoginOpen(true); return }
    setSettingsOpen(true)
  }

  const logout = async () => {
    await signOut()
    setSession(null)
    setSettingsOpen(false)
  }

  return (
    <Box className="app-shell">
      <Container maxWidth="lg" className="page">
        <header className="masthead">
          <Stack direction="row" alignItems="center" spacing={1.2} className="brand-mark">
            <SportsBaseballRounded fontSize="small" />
            <span>DUGOUT LAB</span>
          </Stack>
          <Box className="hero-copy">
            <Typography component="p" className="eyebrow">KBO + MLB DAILY FORECAST · OFFICIAL DATA</Typography>
            <Typography variant="h1">오늘의 경기를<br />숫자로 먼저 읽습니다.</Typography>
            <Typography className="lede">공식 KBO·MLB 기록과 버전 관리된 통계 모델, 20,000회 시뮬레이션으로 만든 투명한 경기 전망.</Typography>
          </Box>
          <Stack direction={{ xs: 'column', sm: 'row' }} spacing={1.2} className="controls">
            <ToggleButtonGroup exclusive size="small" value={league} onChange={(_, value) => value && setLeague(value)} aria-label="리그 필터">
              <ToggleButton value="ALL">전체</ToggleButton><ToggleButton value="KBO">KBO</ToggleButton><ToggleButton value="MLB">MLB</ToggleButton>
            </ToggleButtonGroup>
            <TextField type="date" value={date} onChange={(event) => setDate(event.target.value)} size="small" inputProps={{ 'aria-label': '경기 날짜' }} />
            <Button variant="outlined" startIcon={<RefreshRounded />} onClick={() => void load()} disabled={loading}>다시 불러오기</Button>
            {session ? <>
              <Button variant="outlined" startIcon={<SettingsRounded />} onClick={openClaudeSettings}>내 Claude 설정</Button>
              <Button variant="text" startIcon={<LogoutRounded />} onClick={() => void logout()}>{session.user.email ?? '로그아웃'}</Button>
            </> : <Button variant="outlined" startIcon={<LoginRounded />} onClick={() => setLoginOpen(true)}>로그인</Button>}
          </Stack>
        </header>

        <main>
          <Box className="season-nav">
            <Box><b>{seasonYear} 시즌 경기 아카이브</b><span>한국 날짜 기준 · 저장된 경기일 {seasonDates.length}일</span></Box>
            <DatePicker dates={seasonDates} value={date} league={league} onChange={setDate} />
          </Box>
          {operations && <Box className={`operations-banner ${operations.status}`}>
            <Box><b>{operations.status === 'ok' ? '자동 수집 정상' : '자동 수집 점검 필요'}</b>
              <span>{operations.last_success ? `최근 성공 ${new Date(operations.last_success.finished_at).toLocaleString('ko-KR')}` : '수집 성공 기록 없음'}</span></Box>
            <Box><span>24시간 오류</span><strong>{operations.failures_24h}</strong></Box>
            <Box><span>저장 예측</span><strong>{operations.stored_predictions}</strong></Box>
            <Box><span>변경 알림</span><strong>{operations.change_alerts_24h}</strong></Box>
          </Box>}
          {mobile && <Box className="league-bar">
            <ToggleButtonGroup exclusive size="small" value={league} onChange={(_, value) => value && setLeague(value)} aria-label="리그 필터">
              <ToggleButton value="ALL">전체</ToggleButton><ToggleButton value="KBO">KBO</ToggleButton><ToggleButton value="MLB">MLB</ToggleButton>
            </ToggleButtonGroup>
          </Box>}
          <Stack direction="row" justifyContent="space-between" alignItems="end" className="section-heading">
            <Box>
              <Typography className="eyebrow">MATCH BOARD</Typography>
              <Typography variant="h2">{date.replaceAll('-', '.')} · {league === 'ALL' ? 'KBO + MLB' : league}</Typography>
            </Box>
            <Typography className="game-count">{games.length} GAMES</Typography>
          </Stack>

          {error && <Alert severity="error" sx={{ mb: 3 }} action={<Button color="inherit" size="small" onClick={() => void load()}>재시도</Button>}>{error}</Alert>}
          {loading && games.length === 0 ? <Box className="loading"><CircularProgress size={28} /><Box><b>예측 보드를 불러오는 중</b><span>최대 20초 안에 결과 또는 오류를 표시합니다.</span></Box></Box> : (
            <Box className="game-grid">
              {games.map((game) => <GameCard key={game.id} game={game} signedIn={Boolean(session)} onRequireLogin={() => setLoginOpen(true)} />)}
            </Box>
          )}
          {loading && games.length > 0 && <Box className="refreshing"><CircularProgress size={16} /><span>최신 데이터 확인 중</span></Box>}
          {!loading && !error && games.length === 0 && (
            <Box className="empty-state">
              <SportsBaseballRounded />
              <Typography variant="h6">{date}에 저장된 경기가 없습니다</Typography>
              <Typography color="text.secondary">휴식일이거나 일정 수집 전입니다. 위 시즌 경기 아카이브에서 경기 있는 날짜를 선택해 주세요.</Typography>
            </Box>
          )}
          {!loading && backtest && <Box className="model-board">
            <Box><Typography className="eyebrow">MODEL CHECK</Typography><Typography variant="h3">데이터 누수 없는 성능 평가</Typography></Box>
            {backtest.sample_size && backtest.metrics ? <Box className="metric-grid">
              <Metric label="평가 경기" value={`${backtest.sample_size}`} />
              <Metric label="정확도" value={`${Math.round(backtest.metrics.accuracy * 100)}%`} />
              <Metric label="Brier" value={backtest.metrics.brier_score.toFixed(3)} />
              <Metric label="Log Loss" value={backtest.metrics.log_loss.toFixed(3)} />
              <Metric label="득점 MAE" value={backtest.metrics.runs_mae.toFixed(2)} />
              <Metric label="득점 RMSE" value={backtest.metrics.runs_rmse.toFixed(2)} />
              <Metric label="보정 오차" value={backtest.metrics.calibration_error.toFixed(3)} />
            </Box> : <Typography className="evaluation-empty">
              {backtest.message ?? '평가 데이터가 아직 부족합니다.'}
              {backtest.readiness ? ` 현재 평가 가능 ${backtest.readiness.evaluable_pregame_games}경기 / 예비 판단 ${backtest.readiness.preliminary_minimum}경기 / 권장 ${backtest.readiness.recommended_minimum}경기입니다.` : ''}
            </Typography>}
          </Box>}
        </main>

        <footer>
          <span>DATA · KBO / MLB OFFICIAL</span><span>MODEL · MATCHUP BASELINES</span><span>OPTIONAL CLAUDE · OPTIONAL MARKET API</span>
          <p>모든 확률은 통계적 추정치이며 경기 결과 또는 수익을 보장하지 않습니다.</p>
        </footer>
      </Container>
      {session && <ClaudeSettingsDialog open={settingsOpen} email={session.user.email ?? null} onClose={() => setSettingsOpen(false)} />}
      <LoginDialog open={loginOpen} onClose={() => setLoginOpen(false)} onSignedIn={setSession} />
    </Box>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return <Box><span>{label}</span><strong>{value}</strong></Box>
}
