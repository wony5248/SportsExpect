import { useState } from 'react'
import { Alert, Box, Button, Chip, CircularProgress, Collapse, Divider, LinearProgress, Stack, Typography } from '@mui/material'
import ExpandMoreRounded from '@mui/icons-material/ExpandMoreRounded'
import VerifiedRounded from '@mui/icons-material/VerifiedRounded'
import AutoAwesomeRounded from '@mui/icons-material/AutoAwesomeRounded'
import { fetchPersonalClaudeAnalysis } from '../lib/api'
import { getAccessToken } from '../lib/auth'
import type { Game, PersonalClaudeAnalysis, Team } from '../types'

const pct = (value: number) => `${Math.round(value * 100)}%`
const stat = (value: number | null | undefined, digits = 2) =>
  typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'

export default function GameCard({ game, signedIn, onRequireLogin }: {
  game: Game
  signedIn: boolean
  onRequireLogin: () => void
}) {
  const [open, setOpen] = useState(false)
  const [personalAnalysis, setPersonalAnalysis] = useState<PersonalClaudeAnalysis | null>(null)
  const [personalBusy, setPersonalBusy] = useState(false)
  const [personalError, setPersonalError] = useState<string | null>(null)
  const p = game.prediction
  const coherent = p?.summary_schema_version === 2 && p.coherence_valid === true
  const predictedScore = p?.primary_score ?? p?.top_scores?.[0]
  const expectedScore = p?.display_expected_score ?? (p ? {
    away: Number(p.away_expected_runs.toFixed(1)), home: Number(p.home_expected_runs.toFixed(1)),
  } : undefined)
  const marketLine = game.market?.total_line
  const marketTotal = marketLine == null ? undefined : p?.totals[String(marketLine)]
  const statisticalExpectedTotal = p?.statistical_expected_total ?? (
    typeof p?.home_expected_runs === 'number' && typeof p?.away_expected_runs === 'number'
      ? p.home_expected_runs + p.away_expected_runs
      : undefined
  )
  const favoriteHandicap = !p || !coherent ? undefined : p.home_win_probability >= p.away_win_probability
    ? { team: game.home.name, probability: p.handicap.home_minus_1_5 }
    : { team: game.away.name, probability: p.handicap.away_minus_1_5 }
  const requestPersonalAnalysis = async () => {
    if (!signedIn) { onRequireLogin(); return }
    setPersonalBusy(true); setPersonalError(null)
    try {
      setPersonalAnalysis(await fetchPersonalClaudeAnalysis(game.id, await getAccessToken()))
    } catch (error) {
      setPersonalError(error instanceof Error ? error.message : 'Claude 개인 분석을 불러오지 못했습니다.')
    } finally {
      setPersonalBusy(false)
    }
  }
  const resultComparison = completedGameComparison(game, expectedScore)
  return (
    <article className="game-card">
      <Stack direction="row" justifyContent="space-between" alignItems="center" className="card-meta">
        <span>{game.league === 'MLB'
          ? `한국 ${shortDate(game.date)} ${game.time ?? '시간 미정'} KST · 미국 현지 ${shortDate(game.venue_date)} · ${game.stadium ?? '구장 미정'}`
          : `${game.time ?? '시간 미정'} KST · ${game.stadium ?? '구장 미정'}`}</span>
        <Stack direction="row" spacing={.7}><Chip size="small" label={game.freshness.status === 'FRESH' ? '최신' : '갱신 필요'} className={`freshness ${game.freshness.status.toLowerCase()}`} /><Chip size="small" label={game.status === 'SCHEDULED' ? '경기 예정' : game.status} className={`status ${game.status.toLowerCase()}`} /></Stack>
      </Stack>
      <Box className="matchup">
        <TeamName team={game.away} side="AWAY" />
        <Box className="versus"><span>VS</span><small>{game.league}</small></Box>
        <TeamName team={game.home} side="HOME" />
      </Box>
      {game.result && <Box className="result-comparison">
        <Stack direction="row" justifyContent="space-between" alignItems="baseline" className="result-comparison-heading">
          <b>실제 결과와 경기 전 예측</b>
          <span>{resultComparison ? `저장 예측 · ${new Date(resultComparison.createdAt).toLocaleString('ko-KR')}` : '경기 전 저장 예측 없음'}</span>
        </Stack>
        <Box className="result-comparison-grid">
          <Box className="result-score actual"><span>실제 최종</span><strong>{game.result.away_score} <i>:</i> {game.result.home_score}</strong><small>{game.away.name} : {game.home.name}</small></Box>
          {resultComparison ? <>
            <Box className="result-score predicted"><span>시뮬레이션 평균</span><strong>{stat(resultComparison.awayExpected, 1)} <i>:</i> {stat(resultComparison.homeExpected, 1)}</strong><small>{game.away.name} : {game.home.name}</small></Box>
            <Box className={`result-verdict ${resultComparison.verdictClass}`}><b>{resultComparison.verdict}</b><span>{resultComparison.favorite}</span><small>팀당 평균 점수 오차 {stat(resultComparison.runsMae, 1)}점</small></Box>
          </> : <Box className="result-verdict unavailable"><b>비교할 예측 없음</b><span>경기 시작 전에 저장된 시뮬레이션 예측이 없습니다.</span></Box>}
        </Box>
      </Box>}

      {p && coherent ? <>
        <Box className="probability-block">
          <Stack direction="row" justifyContent="space-between" alignItems="baseline">
            <Box><span className="probability">{pct(p.away_win_probability)}</span><small>{game.away.name}</small></Box>
            <Typography className="metric-label">9이닝 동점 제외 승률</Typography>
            <Box textAlign="right"><span className="probability accent">{pct(p.home_win_probability)}</span><small>{game.home.name}</small></Box>
          </Stack>
          <Box className="probability-track"><i style={{ width: pct(p.away_win_probability) }} /><b style={{ width: pct(p.home_win_probability) }} /></Box>
        </Box>

        <Box className="score-row">
          <Box><span>평균 예상 스코어</span><strong>{stat(expectedScore?.away, 1)} <i>:</i> {stat(expectedScore?.home, 1)}</strong></Box>
          <Divider orientation="vertical" flexItem />
          <Box><span>예상 총점</span><strong>{stat(p.expected_total, 1)}</strong></Box>
          <Divider orientation="vertical" flexItem />
          <Box><span>입력 데이터 완성도</span><strong>{p.confidence}<small>/100</small></strong></Box>
        </Box>

        {p.team_quantiles && p.total_quantiles && p.game_shape ? <Box className="forecast-range">
          <span>시뮬레이션 중앙 80% 득점 구간</span>
          <b>{game.away.name} {stat(p.team_quantiles.away.p10, 0)}–{stat(p.team_quantiles.away.p90, 0)}점 · {game.home.name} {stat(p.team_quantiles.home.p10, 0)}–{stat(p.team_quantiles.home.p90, 0)}점</b>
          <small>총점 중앙 80% {stat(p.total_quantiles.p10, 0)}–{stat(p.total_quantiles.p90, 0)}점 · 9이닝 종료 1점차 이내 {pct(p.game_shape.one_run_probability)} · 5점차 이상 {pct(p.game_shape.blowout_probability)}</small>
        </Box> : null}

        {predictedScore?.inning_line?.length ? <InningLine
          away={game.away.name}
          home={game.home.name}
          score={predictedScore}
        /> : null}

        <Stack direction="row" spacing={1} className="market-row">
          {favoriteHandicap && typeof favoriteHandicap.probability === 'number'
            ? <Box className="market-stat"><span>{favoriteHandicap.team} 2점차 이상 승리</span><b>{pct(favoriteHandicap.probability)}</b><small>-1.5 핸디캡 기준</small></Box>
            : <Box className="market-stat"><span>핸디캡 확률</span><b>미산출</b><small>검증된 값 없음</small></Box>}
          {marketLine != null && marketTotal
            ? <Box className="market-stat"><span>시장 총점 {marketLine}</span><b>초과 {pct(marketTotal.over)}</b><small>미만 {pct(marketTotal.under)}{marketTotal.push ? ` · 동일 ${pct(marketTotal.push)}` : ''}</small></Box>
            : marketLine != null
              ? <Box className="market-stat"><span>시장 총점 {marketLine}</span><b>비교 불가</b><small>모델 지원 기준점 범위 밖</small></Box>
              : <Box className="market-stat"><span>시장 총점</span><b>미수집</b><small>임의 기준점은 표시하지 않음</small></Box>}
          <Chip icon={<VerifiedRounded />} label={completenessLabel(p.confidence_label)} size="small" className={`confidence ${p.confidence_label.toLowerCase()}`} />
        </Stack>
        <Button fullWidth onClick={() => setOpen(!open)} endIcon={<ExpandMoreRounded className={open ? 'rotated' : ''} />} className="detail-button">{open ? '분석 접기' : '상세 분석 보기'}</Button>
        <Collapse in={open}>
          <Box className="details">
            <Typography variant="subtitle2">선발 매치업</Typography>
            <Box className="starter-grid">
              <Starter team={game.away} /><Starter team={game.home} />
            </Box>
            {game.market && <>
              <Typography variant="subtitle2">시장 기준점 비교</Typography>
              <Box className="market-comparison">
                <Box><span>컨센서스 기준 총점</span><strong>{game.market.total_line ?? '—'}</strong><small>{game.market.bookmaker_count}개 북메이커 중앙값</small></Box>
                <Box><span>홈 승률 비교</span><strong>{pct(p.home_win_probability)} <i>/</i> {game.market.home_implied_probability == null ? '—' : pct(game.market.home_implied_probability)}</strong><small>모델 / 마진 제거 시장 확률</small></Box>
                <Box><span>예상 총점 차이</span><strong>{game.market.model_total_difference == null ? '—' : `${game.market.model_total_difference > 0 ? '+' : ''}${game.market.model_total_difference}`}</strong><small>모델 기대총점 - 시장 기준점</small></Box>
              </Box>
              <Typography className="source-note">{game.market.provider} · {new Date(game.market.collected_at).toLocaleString('ko-KR')} · 시장 정보는 비교용이며 추천이 아닙니다.</Typography>
            </>}
            <Typography variant="subtitle2">주요 근거</Typography>
            <ul>{p.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
            <Box className="personal-claude-panel">
              <Stack direction="row" justifyContent="space-between" alignItems="center" spacing={1}>
                <Box><Typography variant="subtitle2">내 Claude 보조 분석</Typography><small>공용 예측은 바꾸지 않고 내 화면에만 표시됩니다.</small></Box>
                <Button variant="outlined" size="small" startIcon={personalBusy ? <CircularProgress size={14} /> : <AutoAwesomeRounded />}
                  onClick={() => void requestPersonalAnalysis()} disabled={personalBusy}>
                  {signedIn ? personalAnalysis ? '다시 분석' : '개인 분석 실행' : '로그인 후 사용'}
                </Button>
              </Stack>
              {personalError && <Alert severity="error" sx={{ mt: 1.5 }}>{personalError}</Alert>}
              {personalAnalysis && <Box className="personal-claude-result">
                <Box><span>{game.away.name}</span><strong>{pct(personalAnalysis.personalized.away_win_probability)}</strong><small>{stat(personalAnalysis.personalized.away_expected_runs, 1)}점</small></Box>
                <Box><span>개인 보정 예상</span><strong>{stat(personalAnalysis.personalized.away_expected_runs, 1)} : {stat(personalAnalysis.personalized.home_expected_runs, 1)}</strong><small>가중치 {stat(personalAnalysis.blend_weight * 100, 1)}%</small></Box>
                <Box><span>{game.home.name}</span><strong>{pct(personalAnalysis.personalized.home_win_probability)}</strong><small>{stat(personalAnalysis.personalized.home_expected_runs, 1)}점</small></Box>
                <ul>{personalAnalysis.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
                {personalAnalysis.caution && <p>{personalAnalysis.caution}</p>}
                <small>{personalAnalysis.model} · {personalAnalysis.disclaimer}</small>
              </Box>}
            </Box>
            <Typography className="source-note">득점 분포 평균 {stat(statisticalExpectedTotal, 2)}점 · 승률·득점·구간·핸디캡·총점 확률은 같은 시뮬레이션 분포에서 계산됩니다. 9이닝 동점 표본은 승률 계산에서 제외합니다.</Typography>
            <Typography variant="subtitle2">최근 10경기</Typography>
            <Box className="comparison"><Compare team={game.away} /><Compare team={game.home} /></Box>
            <Typography variant="subtitle2">{lineupTitle(game)}</Typography>
            <Box className="lineup-grid">
              <Lineup team={game.away.name} entries={game.lineups.away} />
              <Lineup team={game.home.name} entries={game.lineups.home} />
            </Box>
            {game.prediction_timeline.length > 0 && <>
              <Typography variant="subtitle2">예측 변화 타임라인</Typography>
              <Box className="history-list timeline">
                {game.prediction_timeline.slice(-6).reverse().map((item) => <Box key={`${item.captured_at}-${item.stage}`}>
                  <span><i>{stageLabel(item.stage)}</i>{new Date(item.captured_at).toLocaleString('ko-KR')}</span>
                  <b>{game.away.name} {pct(item.away_win_probability)} · {game.home.name} {pct(item.home_win_probability)}<small>{item.changes.map((change) => change.label).join(' ')}</small></b>
                </Box>)}
              </Box>
            </>}
            <Typography variant="subtitle2">가장 자주 나온 정수 스코어</Typography>
            <Stack direction="row" flexWrap="wrap" gap={1} className="score-chips">
              {p.top_scores.slice(0, 5).map((score) => <Chip key={`${score.away}-${score.home}`} label={`${game.away.name} ${score.away} : ${score.home} ${game.home.name} · ${score.probability == null ? '—' : pct(score.probability)}`} />)}
            </Stack>
            <Divider sx={{ my: 2 }} />
            <Typography className="source-note">모델 {p.model.name} · {p.model.simulations.toLocaleString()}회 시뮬레이션 · {new Date(p.created_at).toLocaleString('ko-KR')}</Typography>
            <Typography className="source-note">최근 데이터 갱신 {new Date(game.freshness.last_updated_at).toLocaleString('ko-KR')} · {p.disclaimer}</Typography>
          </Box>
        </Collapse>
      </> : <Box className="no-prediction">{predictionUnavailableMessage(game, Boolean(p))}</Box>}
    </article>
  )
}

function InningLine({ away, home, score }: {
  away: string
  home: string
  score: NonNullable<Game['prediction']>['top_scores'][number]
}) {
  const innings = score.inning_line ?? []
  return <Box className="inning-forecast">
    <Stack direction="row" justifyContent="space-between" alignItems="baseline" className="inning-heading">
      <b>대표 이닝 흐름</b>
      <small>대표 최종 스코어에 해당하는 한 가지 시뮬레이션 표본</small>
    </Stack>
    <Box className="inning-scroll">
      <table>
        <thead><tr><th>팀</th>{innings.map((item) => <th key={item.inning}>{item.inning}</th>)}<th>R</th></tr></thead>
        <tbody>
          <tr><th>{away}</th>{innings.map((item) => <td key={item.inning}>{item.away}</td>)}<td>{score.away}</td></tr>
          <tr><th>{home}</th>{innings.map((item) => <td key={item.inning}>{item.home}</td>)}<td>{score.home}</td></tr>
        </tbody>
      </table>
    </Box>
  </Box>
}

function TeamName({ team, side }: { team: Team; side: string }) {
  const pitcher = team.starter
  return <Box className="team">
    <small>{side}</small>
    <Typography variant="h5">{team.name}</Typography>
    <span>{team.stats ? `${team.stats.wins}승 ${team.stats.losses}패` : '기록 수집 전'}</span>
    <span className="team-starter"><b>선발 {pitcher?.name ?? '미정'}</b><i>{pitcher ? `${pitcher.confirmed ? '확정' : '예상'} · ERA ${stat(pitcher.era)}` : '정보 수집 전'}</i></span>
  </Box>
}

function Starter({ team }: { team: Team }) {
  const p = team.starter
  return <Box><small>{team.name}</small><strong>{p?.name ?? '미정'}</strong><span>ERA {stat(p?.era)} · FIP {stat(p?.fip)} · WHIP {stat(p?.whip)}</span><span>{p?.opponent_innings ? `상대 ${stat(p.opponent_innings, 1)}이닝 · ERA ${stat(p.opponent_era)} · WHIP ${stat(p.opponent_whip)}` : '현재 상대팀 상대 기록 없음'}</span><span>{p?.rest_days != null ? `${p.rest_days}일 휴식` : '휴식 정보 수집 전'}{p?.recent_pitches != null ? ` · 최근 5일 ${p.recent_pitches}구` : ''}</span></Box>
}

function Compare({ team }: { team: Team }) {
  const recent = team.stats?.recent?.['10']
  const value = recent ? recent.win_rate * 100 : 0
  return <Box><Stack direction="row" justifyContent="space-between"><b>{team.name}</b><span>{recent ? `${recent.wins}승 · ${stat(recent.avg_runs, 1)} 득점` : '—'}</span></Stack><LinearProgress variant="determinate" value={value} /></Box>
}

function lineupTitle(game: Game) {
  const entries = [...game.lineups.away, ...game.lineups.home]
  if (!entries.length) return '라인업 발표 전'
  return entries.every((item) => item.confirmed) ? '확정 라인업' : '예상 라인업 · 발표 후 자동 갱신'
}

function Lineup({ team, entries }: { team: string; entries: Game['lineups']['away'] }) {
  return <Box>
    <b>{team}</b>
    {entries.length ? <ol>{entries.map((entry) => <li key={`${entry.order}-${entry.name}`}><span>{entry.name}</span><small>{entry.matchup_plate_appearances ? `${entry.position ?? '—'} · 상대 OPS ${stat(entry.matchup_ops, 3)} (${entry.matchup_plate_appearances}PA)` : entry.position ?? '—'}</small></li>)}</ol> : <p>발표 전</p>}
  </Box>
}

function stageLabel(stage: string) {
  return ({ T_MINUS_24H: '전날', T_MINUS_3H: '3시간 전', T_MINUS_60M: '60분 전', T_MINUS_15M: '15분 전', TIME_UNCONFIRMED: '시간 미정' } as Record<string, string>)[stage] ?? stage
}

function completenessLabel(label: NonNullable<Game['prediction']>['confidence_label']) {
  return ({ HIGH: '정보 충분', MEDIUM: '정보 보통', LOW: '정보 부족' } as const)[label]
}

function predictionUnavailableMessage(game: Game, hasPrediction: boolean) {
  if (game.status === 'CANCELLED') return '취소된 경기로 예측 지표를 표시하지 않습니다.'
  if (hasPrediction && game.result) return '이전 형식의 저장 예측은 위 핵심 비교만 표시합니다. 상세 지표는 현재 예측 형식에서 제공합니다.'
  if (hasPrediction) return '이전 예측 형식은 정합성 검증 후 다시 표시됩니다. 다음 데이터 갱신을 기다려 주세요.'
  if (game.status === 'SCHEDULED') {
    return game.away.stats && game.home.stats
      ? '경기와 팀 기록은 수집됐지만 예측 생성이 아직 완료되지 않았습니다. 다음 자동 갱신 후 표시됩니다.'
      : '예측에 필요한 시즌 팀 기록이 아직 수집되지 않았습니다. 다음 자동 갱신 후 표시됩니다.'
  }
  return '경기 시작 전에 저장된 예측이 없습니다.'
}

function completedGameComparison(game: Game, expectedScore: { away: number; home: number } | undefined) {
  const result = game.result
  const prediction = game.prediction
  if (!result || !prediction || !expectedScore) return null
  const predictedHome = prediction.home_win_probability >= prediction.away_win_probability
  const actualWinner = result.home_score === result.away_score
    ? 'TIE'
    : result.home_score > result.away_score ? 'HOME' : 'AWAY'
  const predictedWinner = predictedHome ? 'HOME' : 'AWAY'
  const favoriteTeam = predictedHome ? game.home.name : game.away.name
  const favoriteProbability = predictedHome ? prediction.home_win_probability : prediction.away_win_probability
  const winnerCorrect = actualWinner === 'TIE' ? null : actualWinner === predictedWinner
  return {
    awayExpected: expectedScore.away,
    homeExpected: expectedScore.home,
    createdAt: prediction.created_at,
    favorite: `${favoriteTeam} 우세 ${pct(favoriteProbability)}`,
    verdict: winnerCorrect == null ? '무승부 · 승패 비교 제외' : winnerCorrect ? '우세팀 적중' : '우세팀 미적중',
    verdictClass: winnerCorrect == null ? 'neutral' : winnerCorrect ? 'correct' : 'incorrect',
    runsMae: (Math.abs(expectedScore.away - result.away_score) + Math.abs(expectedScore.home - result.home_score)) / 2,
  }
}

function shortDate(value: string) {
  const [, month, day] = value.split('-')
  return `${month}.${day}`
}
