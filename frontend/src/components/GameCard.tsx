import { useState } from 'react'
import { Box, Button, Chip, Collapse, Divider, LinearProgress, Stack, Typography } from '@mui/material'
import ExpandMoreRounded from '@mui/icons-material/ExpandMoreRounded'
import VerifiedRounded from '@mui/icons-material/VerifiedRounded'
import type { Game, Team } from '../types'

const pct = (value: number) => `${Math.round(value * 100)}%`
const stat = (value: number | null | undefined, digits = 2) =>
  typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—'

export default function GameCard({ game }: { game: Game }) {
  const [open, setOpen] = useState(false)
  const p = game.prediction
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
  return (
    <article className="game-card">
      <Stack direction="row" justifyContent="space-between" alignItems="center" className="card-meta">
        <span>{game.time ?? '시간 미정'} KST · {game.stadium ?? '구장 미정'}</span>
        <Stack direction="row" spacing={.7}><Chip size="small" label={game.freshness.status === 'FRESH' ? '최신' : '갱신 필요'} className={`freshness ${game.freshness.status.toLowerCase()}`} /><Chip size="small" label={game.status === 'SCHEDULED' ? '경기 예정' : game.status} className={`status ${game.status.toLowerCase()}`} /></Stack>
      </Stack>
      <Box className="matchup">
        <TeamName team={game.away} side="AWAY" />
        <Box className="versus"><span>VS</span><small>{game.league}</small></Box>
        <TeamName team={game.home} side="HOME" />
      </Box>
      {game.result && <Box className="final-score"><span>최종 스코어</span><strong>{game.result.away_score} <i>:</i> {game.result.home_score}</strong></Box>}

      {p ? <>
        <Box className="probability-block">
          <Stack direction="row" justifyContent="space-between" alignItems="baseline">
            <Box><span className="probability">{pct(p.away_win_probability)}</span><small>{game.away.name}</small></Box>
            <Typography className="metric-label">승리 확률</Typography>
            <Box textAlign="right"><span className="probability accent">{pct(p.home_win_probability)}</span><small>{game.home.name}</small></Box>
          </Stack>
          <Box className="probability-track"><i style={{ width: pct(p.away_win_probability) }} /><b style={{ width: pct(p.home_win_probability) }} /></Box>
        </Box>

        <Box className="score-row">
          <Box><span>평균 예상 스코어</span><strong>{stat(expectedScore?.away, 1)} <i>:</i> {stat(expectedScore?.home, 1)}</strong></Box>
          <Divider orientation="vertical" flexItem />
          <Box><span>예상 총점</span><strong>{stat(p.expected_total, 1)}</strong></Box>
          <Divider orientation="vertical" flexItem />
          <Box><span>신뢰도</span><strong>{p.confidence}<small>/100</small></strong></Box>
        </Box>

        <Box className="forecast-range">
          <span>80% 예상 범위</span>
          <b>{game.away.name} {p.team_quantiles ? `${p.team_quantiles.away.p10}–${p.team_quantiles.away.p90}` : '—'} · {game.home.name} {p.team_quantiles ? `${p.team_quantiles.home.p10}–${p.team_quantiles.home.p90}` : '—'}</b>
          <small>총점 {p.total_quantiles ? `${p.total_quantiles.p10}–${p.total_quantiles.p90}` : '—'} · 5점차 이상 {p.game_shape ? pct(p.game_shape.blowout_probability) : '—'}</small>
        </Box>

        {predictedScore?.inning_line?.length ? <InningLine
          away={game.away.name}
          home={game.home.name}
          score={predictedScore}
        /> : null}

        <Stack direction="row" spacing={1} className="market-row">
          <Box className="market-stat"><span>{game.home.name} -1.5</span><b>{pct(p.handicap.home_minus_1_5)}</b></Box>
          <Box className="market-stat"><span>{marketLine != null ? `시장 O ${marketLine}` : '모델 O 8.5'}</span><b>{pct(marketTotal?.over ?? p.totals['8.5'].over)}</b></Box>
          <Chip icon={<VerifiedRounded />} label={p.confidence_label} size="small" className={`confidence ${p.confidence_label.toLowerCase()}`} />
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
            <Typography className="source-note">득점 분포 평균 {stat(statisticalExpectedTotal, 2)}점 · 평균 예상 스코어와 예상 총점은 같은 기대값에서 계산됩니다.</Typography>
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
            {p.ai_assist && <Typography className="source-note">Claude 보조 {p.ai_assist.used ? `${p.ai_assist.model ?? 'AI'} 적용 · ${p.ai_assist.blend_weight == null ? '가중치 정보 없음' : `가중치 ${stat(p.ai_assist.blend_weight * 100, 1)}%`}` : '미사용'} · 통계 모델 실패 시 자동 대체</Typography>}
            <Typography className="source-note">최근 데이터 갱신 {new Date(game.freshness.last_updated_at).toLocaleString('ko-KR')} · {p.disclaimer}</Typography>
          </Box>
        </Collapse>
      </> : <Box className="no-prediction">{game.status === 'SCHEDULED' ? '예측 입력 데이터가 아직 충분하지 않습니다.' : '경기 시작 전에 저장된 예측이 없습니다.'}</Box>}
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
      <b>대표 이닝별 예상</b>
      <small>대표 최종점수 조건에서 가장 자주 나온 흐름</small>
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
  return <Box className="team"><small>{side}</small><Typography variant="h5">{team.name}</Typography><span>{team.stats ? `${team.stats.wins}승 ${team.stats.losses}패` : '기록 수집 전'}</span></Box>
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
