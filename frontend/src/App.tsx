import { useCallback, useEffect, useState } from 'react'
import { Alert, Box, Button, CircularProgress, Container, Stack, TextField, ToggleButton, ToggleButtonGroup, Typography } from '@mui/material'
import RefreshRounded from '@mui/icons-material/RefreshRounded'
import SettingsRounded from '@mui/icons-material/SettingsRounded'
import SportsBaseballRounded from '@mui/icons-material/SportsBaseballRounded'
import { fetchBacktest, fetchGames, fetchOperations, kstToday } from './lib/api'
import type { Backtest, Game, OperationsStatus } from './types'
import GameCard from './components/GameCard'
import ClaudeSettingsDialog from './components/ClaudeSettingsDialog'

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

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const [gameRows, operationStatus, evaluation] = await Promise.all([
        fetchGames(date, league), fetchOperations(), fetchBacktest(league),
      ])
      setGames(gameRows); setOperations(operationStatus); setBacktest(evaluation)
    }
    catch (err) { setError(err instanceof Error ? err.message : '알 수 없는 오류') }
    finally { setLoading(false) }
  }, [date, league])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    const query = new URLSearchParams({ date, league })
    window.history.replaceState(null, '', `${window.location.pathname}?${query}`)
  }, [date, league])

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
            <Button variant="outlined" startIcon={<SettingsRounded />} onClick={() => setSettingsOpen(true)}>Claude 설정</Button>
          </Stack>
        </header>

        <main>
          {operations && <Box className={`operations-banner ${operations.status}`}>
            <Box><b>{operations.status === 'ok' ? '자동 수집 정상' : '자동 수집 점검 필요'}</b>
              <span>{operations.last_success ? `최근 성공 ${new Date(operations.last_success.finished_at).toLocaleString('ko-KR')}` : '수집 성공 기록 없음'}</span></Box>
            <Box><span>24시간 오류</span><strong>{operations.failures_24h}</strong></Box>
            <Box><span>저장 예측</span><strong>{operations.stored_predictions}</strong></Box>
            <Box><span>변경 알림</span><strong>{operations.change_alerts_24h}</strong></Box>
          </Box>}
          <Stack direction="row" justifyContent="space-between" alignItems="end" className="section-heading">
            <Box>
              <Typography className="eyebrow">MATCH BOARD</Typography>
              <Typography variant="h2">{date.replaceAll('-', '.')} · {league === 'ALL' ? 'KBO + MLB' : league}</Typography>
            </Box>
            <Typography className="game-count">{games.length} GAMES</Typography>
          </Stack>

          {error && <Alert severity="error" sx={{ mb: 3 }}>{error}<br />백엔드 실행과 해당 날짜의 refresh 여부를 확인하세요.</Alert>}
          {loading ? <Box className="loading"><CircularProgress size={28} /><span>예측 보드를 불러오는 중</span></Box> : (
            <Box className="game-grid">
              {games.map((game) => <GameCard key={game.id} game={game} />)}
            </Box>
          )}
          {!loading && !error && games.length === 0 && (
            <Box className="empty-state">
              <SportsBaseballRounded />
              <Typography variant="h6">저장된 경기가 없습니다</Typography>
              <Typography color="text.secondary">Supabase Cron의 첫 수집이 완료되지 않았거나 해당 날짜에 경기가 없습니다.</Typography>
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
      <ClaudeSettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </Box>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return <Box><span>{label}</span><strong>{value}</strong></Box>
}
