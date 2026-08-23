import { useState } from 'react'
import { Alert, Box, Button, Chip, CircularProgress, Collapse, Divider, LinearProgress, Stack, Typography } from '@mui/material'
import ExpandMoreRounded from '@mui/icons-material/ExpandMoreRounded'
import VerifiedRounded from '@mui/icons-material/VerifiedRounded'
import AutoAwesomeRounded from '@mui/icons-material/AutoAwesomeRounded'
import { fetchPersonalClaudeAnalysis } from '../lib/api'
import { getAccessToken } from '../lib/auth'
import type { Game, PersonalClaudeAnalysis, Team } from '../types'

const pct = (value: number) => `${Math.round(value * 100)}%`
const pctFine = (value: number) => `${(value * 100).toFixed(1)}%`

// Relief groups in the order a manager works through them, worst leverage last.
const BULLPEN_TIERS = [
  { key: 'starter', share: 'starter_share', label: '선발' },
  { key: 'high', share: 'high_leverage_share', label: '필승조' },
  { key: 'middle', share: 'middle_share', label: '중간' },
  { key: 'chase', share: 'chase_share', label: '추격조' },
  { key: 'mop', share: 'mop_up_share', label: '등봉조' },
] as const
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
  const coherent = Boolean(p && (p.summary_schema_version ?? 0) >= 2 && p.coherence_valid === true)
  const predictedScore = p?.top_scores?.[0] ?? p?.primary_score
  const expectedScore = predictedScore ? { away: predictedScore.away, home: predictedScore.home } : p?.display_expected_score ?? (p ? {
    away: Number(p.away_expected_runs.toFixed(1)), home: Number(p.home_expected_runs.toFixed(1)),
  } : undefined)
  const meanScore = p?.score_estimates?.mean ?? (p ? {
    away: Number(p.away_expected_runs.toFixed(1)), home: Number(p.home_expected_runs.toFixed(1)),
  } : undefined)
  const weightedScore = p?.score_estimates?.top5_weighted
  const modeTotal = p?.simulation_modes?.total_runs
  const modeOutcome = p?.simulation_modes?.outcome
  const statisticalExpectedTotal = p?.statistical_expected_total ?? (
    typeof p?.home_expected_runs === 'number' && typeof p?.away_expected_runs === 'number'
      ? p.home_expected_runs + p.away_expected_runs
      : undefined
  )
  const homeFavored = !p || p.home_win_probability >= p.away_win_probability
  const ranking = p && coherent ? rankedOutcomes(p, game) : null
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
  const verdicts = game.result && p && ranking ? marketVerdicts(p, game, ranking) : null
  const judgedVerdicts = verdicts?.filter((verdict) => verdict.hit != null) ?? []
  const verdictHits = judgedVerdicts.filter((verdict) => verdict.hit).length
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
          <b>경기 전 예측은 맞았을까{judgedVerdicts.length ? ` · ${judgedVerdicts.length}개 중 ${verdictHits}개 적중` : ''}</b>
          <span>{resultComparison ? `${new Date(resultComparison.createdAt).toLocaleString('ko-KR')}에 저장한 예측` : '경기 전 저장한 예측 없음'}</span>
        </Stack>
        <Box className="result-comparison-grid">
          <Box className="result-score actual"><span>실제 최종</span><strong>{game.result.away_score} <i>:</i> {game.result.home_score}</strong><small>{game.away.name} : {game.home.name}</small></Box>
          {resultComparison ? <>
            <Box className="result-score predicted"><span>가장 많이 나온 점수</span><strong>{stat(resultComparison.awayExpected, 0)} <i>:</i> {stat(resultComparison.homeExpected, 0)}</strong><small>{game.away.name} : {game.home.name}{p?.extra_innings ? '' : ' · 9이닝만 계산한 이전 모델'}</small></Box>
            <Box className={`result-verdict ${resultComparison.verdictClass}`}><b>{resultComparison.verdict}</b><span>{resultComparison.favorite}</span><small>이 점수 기준 팀당 {stat(resultComparison.runsMae, 1)}점 차이</small></Box>
          </> : <Box className="result-verdict unavailable"><b>비교할 예측 없음</b><span>경기 시작 전에 저장해 둔 예측이 없습니다.</span></Box>}
        </Box>
        {verdicts && verdicts.length > 0 && <Box className="market-verdicts">
          {verdicts.map((verdict) => <Box key={verdict.market} className={`market-verdict ${verdict.hit == null ? 'neutral' : verdict.hit ? 'hit' : 'miss'}`}>
            <span>{verdict.market}</span>
            <b>{verdict.hit == null ? '판정 제외' : verdict.hit ? '적중' : '미적중'}</b>
            <small>예측 {verdict.pick} ({pct(verdict.probability)}) · 실제 {verdict.actual}</small>
          </Box>)}
        </Box>}
      </Box>}

      {p && coherent ? <>
        <Box className="probability-block">
          <Stack direction="row" justifyContent="space-between" alignItems="baseline">
            <Box><span className={`probability${homeFavored ? '' : ' accent'}`}>{pct(p.away_win_probability)}</span><small>{game.away.name}{homeFavored ? '' : ' · 우세'}</small></Box>
            <Typography className="metric-label">{p.extra_innings
              ? game.league === 'MLB' ? '이길 확률 · 연장 승부치기까지 포함' : '이길 확률 · 무승부 제외'
              : '이길 확률 · 무승부 제외'}</Typography>
            <Box textAlign="right"><span className={`probability${homeFavored ? ' accent' : ''}`}>{pct(p.home_win_probability)}</span><small>{game.home.name}{homeFavored ? ' · 우세' : ''}</small></Box>
          </Stack>
          <Box className="probability-track"><i style={{ width: pct(p.away_win_probability) }} /><b style={{ width: pct(p.home_win_probability) }} /></Box>
        </Box>

        <Box className="score-row">
          <Box className="primary"><span>예상 점수 · 평균</span><strong>{stat(meanScore?.away, 1)} <i>:</i> {stat(meanScore?.home, 1)}</strong><small>{weightedScore ? `자주 나온 5개 점수 평균 ${stat(weightedScore.away, 1)} : ${stat(weightedScore.home, 1)} (전체의 ${pct(weightedScore.coverage_probability)})` : `평균 총점 ${stat(statisticalExpectedTotal, 1)}점`}</small></Box>
          <Divider orientation="vertical" flexItem />
          <Box><span>가장 많이 나온 점수</span><strong>{stat(expectedScore?.away, 0)} <i>:</i> {stat(expectedScore?.home, 0)}</strong><small>{modeFrequency(predictedScore)}{p.extra_innings ? '' : ' · 9이닝만 계산한 이전 모델'}</small></Box>
          <Divider orientation="vertical" flexItem />
          <Box><span>{ranking ? '가장 유력한 결과' : modeOutcome ? '가장 많이 나온 결과' : '확률이 높은 결과'}</span><strong>{ranking ? ranking.outcomes[0].label : modeOutcome ? outcomeLabel(modeOutcome.value, game) : favoriteLabel(p, game)}</strong><small>{ranking ? `${pct(ranking.outcomes[0].probability)}${ranking.outcomes[0].note ? ` · ${ranking.outcomes[0].note}` : ''}` : modeOutcome ? modeFrequency(modeOutcome) : pct(Math.max(p.home_win_probability, p.away_win_probability))}</small></Box>
        </Box>

        {p.top_scores?.length ? <Box className="score-candidates">
          <span>나올 만한 최종 점수 3가지</span>
          <Stack direction="row" flexWrap="wrap" gap={.8}>
            {p.top_scores.slice(0, 3).map((score, index) => <b key={`${score.away}-${score.home}`} className={index === 0 ? 'top' : ''}>
              <small className="rank">{index + 1}위</small>{score.away} : {score.home}<small>{score.probability == null ? '—' : pctFine(score.probability)}</small>
            </b>)}
            {modeTotal ? <b className="mode-total">총점은 {modeTotal.value}점이 최다<small>{pctFine(modeTotal.probability)}</small></b> : null}
          </Stack>
        </Box> : null}

        {p.team_dense_intervals && p.total_dense_interval && p.game_shape ? <Box className="forecast-range">
          <Stack direction="row" justifyContent="space-between" alignItems="baseline">
            <span>팀별 예상 득점대</span>
            <small className="range-caption">가장 자주 나온 점수대 · 세로선은 평균</small>
          </Stack>
          <ScoreRangeBar name={game.away.name} interval={p.team_dense_intervals.away} mean={meanScore?.away}
            scale={rangeScale(p.team_dense_intervals, meanScore)} />
          <ScoreRangeBar name={game.home.name} interval={p.team_dense_intervals.home} mean={meanScore?.home}
            scale={rangeScale(p.team_dense_intervals, meanScore)} />
          <small>총점 {p.total_dense_interval.low}–{p.total_dense_interval.high}점 ({pct(p.total_dense_interval.mass)}) · 1점차 이내 접전 {pct(p.game_shape.one_run_probability)} · 5점차 이상 대승 {pct(p.game_shape.blowout_probability)}{p.extra_innings ? ` · 연장 갈 확률 ${pct(p.extra_innings.probability)}` : ''}{p.extra_innings && game.league !== 'MLB' && p.tie_probability > 0 ? ` · 무승부 ${pctFine(p.tie_probability)}` : ''}</small>
        </Box> : p.team_quantiles && p.total_quantiles && p.game_shape ? <Box className="forecast-range">
          <span>팀별 예상 득점대 · 중간 80%</span>
          <b>{game.away.name} {stat(p.team_quantiles.away.p10, 0)}–{stat(p.team_quantiles.away.p90, 0)}점 · {game.home.name} {stat(p.team_quantiles.home.p10, 0)}–{stat(p.team_quantiles.home.p90, 0)}점</b>
          <small>총점 {stat(p.total_quantiles.p10, 0)}–{stat(p.total_quantiles.p90, 0)}점 · 1점차 이내 접전 {pct(p.game_shape.one_run_probability)} · 5점차 이상 대승 {pct(p.game_shape.blowout_probability)}</small>
        </Box> : null}

        {predictedScore?.inning_line?.length ? <InningLine
          away={game.away.name}
          home={game.home.name}
          score={predictedScore}
        /> : null}

        {ranking && <Box className="outcome-ranking">
          <Stack direction="row" justifyContent="space-between" alignItems="center" className="ranking-heading">
            <b>유력한 결과 순위</b>
            <Chip icon={<VerifiedRounded />} label={completenessLabel(p.confidence_label)} size="small" className={`confidence ${p.confidence_label.toLowerCase()}`} />
          </Stack>
          <small className="ranking-note">{p.model.simulations.toLocaleString()}번 돌려본 결과를 확률 높은 순으로 · 승패 / 핸디캡 ±1.5 / 총점 {ranking.line != null ? `${ranking.line} 기준 (${ranking.lineSource})` : '기준점 없음'}</small>
          {ranking.outcomes.map((outcome, index) => <Box key={outcome.label} className={`outcome-row${index === 0 ? ' top' : ''}`}>
            <i>{index + 1}</i>
            <Box className="outcome-label"><span>{outcome.label}</span>{outcome.note && <small>{outcome.note}</small>}</Box>
            <Box className="outcome-track"><b style={{ width: pct(outcome.probability) }} /></Box>
            <strong>{pct(outcome.probability)}</strong>
          </Box>)}
        </Box>}
        <Button fullWidth onClick={() => setOpen(!open)} endIcon={<ExpandMoreRounded className={open ? 'rotated' : ''} />} className="detail-button">{open ? '분석 접기' : '상세 분석 보기'}</Button>
        <Collapse in={open}>
          <Box className="details">
            <Typography variant="subtitle2">선발 매치업</Typography>
            <Box className="starter-grid">
              <Starter team={game.away} /><Starter team={game.home} />
            </Box>
            {game.market && <>
              <Typography variant="subtitle2">시장 기준점과 비교</Typography>
              <Box className="market-comparison">
                <Box><span>시장 총점 기준</span><strong>{game.market.total_line ?? '—'}</strong><small>북메이커 {game.market.bookmaker_count}곳의 중간값</small></Box>
                <Box><span>시장 핸디캡 (마핸)</span><strong>{game.market.home_spread == null ? '—'
                  : game.market.home_spread < 0 ? `${game.home.name} ${game.market.home_spread}`
                  : game.market.home_spread > 0 ? `${game.away.name} ${-game.market.home_spread}`
                  : '핸디 없음'}</strong><small>{game.market.home_spread == null ? '아직 수집 전'
                  : game.market.home_spread === 0 ? '시장도 대등하게 평가'
                  : (game.market.home_spread < 0) === (p.home_win_probability >= p.away_win_probability)
                    ? '우리 모델과 같은 팀' : '우리 모델과 다른 팀'}</small></Box>
                <Box><span>홈 승률</span><strong>{pct(p.home_win_probability)} <i>/</i> {game.market.home_implied_probability == null ? '—' : pct(game.market.home_implied_probability)}</strong><small>우리 모델 / 시장 (수수료 제외)</small></Box>
                <Box><span>총점 시각차</span><strong>{game.market.model_total_difference == null ? '—' : `${game.market.model_total_difference > 0 ? '+' : ''}${game.market.model_total_difference}`}</strong><small>우리 평균 총점이 시장 기준보다 {game.market.model_total_difference == null ? '—' : game.market.model_total_difference > 0 ? '높음' : game.market.model_total_difference < 0 ? '낮음' : '같음'}</small></Box>
              </Box>
              <Typography className="source-note">{game.market.provider} · {new Date(game.market.collected_at).toLocaleString('ko-KR')} · 시장 정보는 비교용으로만 보여드리며 베팅 추천이 아닙니다.</Typography>
            </>}
            {p.bullpen_usage && <>
              <Typography variant="subtitle2">마운드 운영 예상</Typography>
              <Box className="bullpen-grid">
                {(['away', 'home'] as const).map((side) => {
                  const usage = p.bullpen_usage![side]
                  return <Box key={side} className="bullpen-team">
                    <Stack direction="row" justifyContent="space-between" alignItems="baseline">
                      <b>{game[side].name}</b><small>선발 {usage.starter_innings}이닝 예상</small>
                    </Stack>
                    <Box className="bullpen-bar">
                      {BULLPEN_TIERS.map((tier) => usage[tier.share] > 0 && <i key={tier.share} className={tier.key}
                        style={{ width: pct(usage[tier.share]) }} title={`${tier.label} ${pct(usage[tier.share])}`} />)}
                    </Box>
                    <Box className="bullpen-legend">
                      {BULLPEN_TIERS.map((tier) => <span key={tier.share} className={tier.key}>
                        {tier.label} {pct(usage[tier.share])}
                      </span>)}
                    </Box>
                  </Box>
                })}
              </Box>
              <Typography className="source-note">{p.engine === 'PLATE_APPEARANCE'
                ? `타자 한 명 한 명이 타석에 들어서는 방식으로 계산했습니다. 타자별 주자 상황 기록(득점권 포함)을 반영했고, 라인업 9명 중 ${p.split_coverage ? `${game.away.name} ${p.split_coverage.away}명 · ${game.home.name} ${p.split_coverage.home}명` : '일부'}은 본인 기록을 그대로 썼습니다.`
                : '이번 예측은 이닝 단위로 계산했습니다. 라인업이 발표되고 타자별 기록이 모이면 타석 단위 계산으로 자동 전환됩니다.'}</Typography>
            </>}
            <Typography variant="subtitle2">주요 근거</Typography>
            <ul>{p.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
            <Box className="personal-claude-panel">
              <Stack direction="row" justifyContent="space-between" alignItems="center" spacing={1}>
                <Box><Typography variant="subtitle2">내 Claude 보조 분석</Typography><small>위 예측은 그대로 두고 내 화면에만 따로 보여드립니다.</small></Box>
                <Button variant="outlined" size="small" startIcon={personalBusy ? <CircularProgress size={14} /> : <AutoAwesomeRounded />}
                  onClick={() => void requestPersonalAnalysis()} disabled={personalBusy}>
                  {signedIn ? personalAnalysis ? '다시 분석' : '개인 분석 실행' : '로그인 후 사용'}
                </Button>
              </Stack>
              {personalError && <Alert severity="error" sx={{ mt: 1.5 }}>{personalError}</Alert>}
              {personalAnalysis && <Box className="personal-claude-result">
                <Box><span>{game.away.name}</span><strong>{pct(personalAnalysis.personalized.away_win_probability)}</strong><small>{stat(personalAnalysis.personalized.away_expected_runs, 1)}점</small></Box>
                <Box><span>내 분석 반영 점수</span><strong>{stat(personalAnalysis.personalized.away_expected_runs, 1)} : {stat(personalAnalysis.personalized.home_expected_runs, 1)}</strong><small>반영 비율 {stat(personalAnalysis.blend_weight * 100, 1)}%</small></Box>
                <Box><span>{game.home.name}</span><strong>{pct(personalAnalysis.personalized.home_win_probability)}</strong><small>{stat(personalAnalysis.personalized.home_expected_runs, 1)}점</small></Box>
                <ul>{personalAnalysis.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
                {personalAnalysis.caution && <p>{personalAnalysis.caution}</p>}
                <small>{personalAnalysis.model} · {personalAnalysis.disclaimer}</small>
              </Box>}
            </Box>
            <Typography className="source-note">이 경기를 {p.model.simulations.toLocaleString()}번 가상으로 치러본 결과입니다. 평균 점수는 그 {p.model.simulations.toLocaleString()}번의 평균이고, 가장 많이 나온 점수는 실제로 제일 자주 나온 최종 점수입니다. 승률·득점대·총점도 모두 같은 계산에서 나옵니다. 평균 총점은 {stat(statisticalExpectedTotal, 2)}점입니다. {p.extra_innings
              ? game.league === 'MLB'
                ? '연장은 실제 MLB 규정대로 승부치기(무사 2루 주자)를 적용해 승부가 날 때까지 치르기 때문에 무승부가 없습니다.'
                : '연장은 실제 KBO 규정대로 승부치기 없이 11회까지만 치르고, 그래도 동점이면 무승부로 두고 승률 계산에서 뺍니다.'
              : '동점으로 끝난 경우는 승률 계산에서 뺍니다.'}</Typography>
            <Typography variant="subtitle2">최근 10경기</Typography>
            <Box className="comparison"><Compare team={game.away} /><Compare team={game.home} /></Box>
            <Typography variant="subtitle2">{lineupTitle(game)}</Typography>
            <Box className="lineup-grid">
              <Lineup team={game.away.name} entries={game.lineups.away} />
              <Lineup team={game.home.name} entries={game.lineups.home} />
            </Box>
            {game.prediction_timeline.length > 0 && <>
              <Typography variant="subtitle2">예측이 바뀌어 온 과정</Typography>
              <Box className="history-list timeline">
                {game.prediction_timeline.slice(-6).reverse().map((item) => <Box key={`${item.captured_at}-${item.stage}`}>
                  <span><i>{stageLabel(item.stage)}</i>{new Date(item.captured_at).toLocaleString('ko-KR')}</span>
                  <b>{game.away.name} {pct(item.away_win_probability)} · {game.home.name} {pct(item.home_win_probability)}<small>{item.changes.map((change) => change.label).join(' ')}</small></b>
                </Box>)}
              </Box>
            </>}
            <Typography variant="subtitle2">자주 나온 최종 점수</Typography>
            <Stack direction="row" flexWrap="wrap" gap={1} className="score-chips">
              {p.top_scores.slice(0, 5).map((score) => <Chip key={`${score.away}-${score.home}`} label={`${game.away.name} ${score.away} : ${score.home} ${game.home.name} · ${score.count == null ? '' : `${score.count.toLocaleString()}번 · `}${score.probability == null ? '—' : pct(score.probability)}`} />)}
            </Stack>
            <Divider sx={{ my: 2 }} />
            <Typography className="source-note">{p.model.name} 모델 · {p.model.simulations.toLocaleString()}번 계산 · 예측 생성 {new Date(p.created_at).toLocaleString('ko-KR')}</Typography>
            <Typography className="source-note">데이터 마지막 갱신 {new Date(game.freshness.last_updated_at).toLocaleString('ko-KR')} · {p.disclaimer}</Typography>
          </Box>
        </Collapse>
      </> : <Box className="no-prediction">{predictionUnavailableMessage(game, Boolean(p))}</Box>}
    </article>
  )
}

function rangeScale(intervals: NonNullable<NonNullable<Game['prediction']>['team_dense_intervals']>,
                    mean: { away: number; home: number } | undefined) {
  return Math.max(intervals.away.high, intervals.home.high, mean?.away ?? 0, mean?.home ?? 0) + 2
}

function ScoreRangeBar({ name, interval, mean, scale }: {
  name: string
  interval: { low: number; high: number; mass: number }
  mean: number | undefined
  scale: number
}) {
  const left = (interval.low / scale) * 100
  const width = Math.max(3, ((interval.high - interval.low + 1) / scale) * 100)
  return <Box className="range-bar">
    <b>{name}</b>
    <Box className="range-track">
      <i style={{ left: `${left}%`, width: `${width}%` }} />
      {typeof mean === 'number' && <em style={{ left: `${Math.min(99, (mean / scale) * 100)}%` }} />}
    </Box>
    <span>{interval.low}–{interval.high}점 <small>({pct(interval.mass)})</small></span>
  </Box>
}

function InningLine({ away, home, score }: {
  away: string
  home: string
  score: NonNullable<Game['prediction']>['top_scores'][number]
}) {
  const innings = score.inning_line ?? []
  return <Box className="inning-forecast">
    <Stack direction="row" justifyContent="space-between" alignItems="baseline" className="inning-heading">
      <b>이렇게 흘러갈 가능성이 큽니다</b>
      <small>{score.count == null ? '가장 많이 나온 최종 점수 기준' : `이 점수로 끝난 ${score.count.toLocaleString()}번 중 이 이닝 흐름이 ${score.trajectory_count?.toLocaleString() ?? '가장 많이'}번`}</small>
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
  return <Box><small>{team.name}</small><strong>{p?.name ?? '미정'}</strong><span>ERA {stat(p?.era)} · FIP {stat(p?.fip)} · WHIP {stat(p?.whip)}</span><span>{p?.opponent_innings ? `오늘 상대팀 상대로 ${stat(p.opponent_innings, 1)}이닝 · ERA ${stat(p.opponent_era)} · WHIP ${stat(p.opponent_whip)}` : '오늘 상대팀과 맞붙은 기록 없음'}</span><span>{p?.rest_days != null ? `${p.rest_days}일 쉬고 등판` : '휴식일 정보 없음'}{p?.recent_pitches != null ? ` · 최근 5일 ${p.recent_pitches}구` : ''}</span></Box>
}

function Compare({ team }: { team: Team }) {
  const recent = team.stats?.recent?.['10']
  const value = recent ? recent.win_rate * 100 : 0
  return <Box><Stack direction="row" justifyContent="space-between"><b>{team.name}</b><span>{recent ? `${recent.wins}승 · 경기당 ${stat(recent.avg_runs, 1)}득점` : '—'}</span></Stack><LinearProgress variant="determinate" value={value} /></Box>
}

function lineupTitle(game: Game) {
  const entries = [...game.lineups.away, ...game.lineups.home]
  if (!entries.length) return '라인업 발표 전'
  return entries.every((item) => item.confirmed) ? '발표된 라인업' : '예상 라인업 · 발표되면 자동으로 바뀝니다'
}

function Lineup({ team, entries }: { team: string; entries: Game['lineups']['away'] }) {
  return <Box>
    <b>{team}</b>
    {entries.length ? <ol>{entries.map((entry) => <li key={`${entry.order}-${entry.name}`}><span>{entry.name}</span><small>{entry.matchup_plate_appearances ? `${entry.position ?? '—'} · 오늘 선발 상대 OPS ${stat(entry.matchup_ops, 3)} (${entry.matchup_plate_appearances}타석)` : entry.position ?? '—'}</small></li>)}</ol> : <p>발표 전</p>}
  </Box>
}

function stageLabel(stage: string) {
  return ({ T_MINUS_24H: '전날', T_MINUS_3H: '3시간 전', T_MINUS_60M: '60분 전', T_MINUS_15M: '15분 전', TIME_UNCONFIRMED: '시간 미정' } as Record<string, string>)[stage] ?? stage
}

function completenessLabel(label: NonNullable<Game['prediction']>['confidence_label']) {
  return ({ HIGH: '정보 충분', MEDIUM: '정보 보통', LOW: '정보 부족' } as const)[label]
}

function predictionUnavailableMessage(game: Game, hasPrediction: boolean) {
  if (game.status === 'CANCELLED') return '취소된 경기라 예측을 표시하지 않습니다.'
  if (hasPrediction && game.result) return '예전 방식으로 저장된 예측이라 위 비교만 보여드립니다. 자세한 지표는 최신 예측부터 제공됩니다.'
  if (hasPrediction) return '예전 방식으로 저장된 예측이라 확인 후 다시 표시됩니다. 다음 갱신을 기다려 주세요.'
  if (game.status === 'SCHEDULED') {
    return game.away.stats && game.home.stats
      ? '경기 일정과 팀 기록은 모았지만 예측 계산이 아직 끝나지 않았습니다. 다음 갱신 때 표시됩니다.'
      : '예측에 필요한 시즌 팀 기록을 아직 모으는 중입니다. 다음 갱신 때 표시됩니다.'
  }
  return '경기 시작 전에 저장해 둔 예측이 없습니다.'
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
    favorite: `${favoriteTeam} 우세로 봤음 ${pct(favoriteProbability)}`,
    verdict: winnerCorrect == null ? '무승부 · 비교 제외' : winnerCorrect ? '승패 맞힘' : '승패 틀림',
    verdictClass: winnerCorrect == null ? 'neutral' : winnerCorrect ? 'correct' : 'incorrect',
    runsMae: (Math.abs(expectedScore.away - result.away_score) + Math.abs(expectedScore.home - result.home_score)) / 2,
  }
}

type RankedOutcome = { label: string; probability: number; note?: string }

type MarketVerdict = { market: string; pick: string; probability: number; actual: string; hit: boolean | null }

function marketVerdicts(p: NonNullable<Game['prediction']>, game: Game,
                        ranking: ReturnType<typeof rankedOutcomes>): MarketVerdict[] {
  const result = game.result
  if (!result) return []
  const margin = result.home_score - result.away_score
  const total = result.home_score + result.away_score
  const homeFavored = p.home_win_probability >= p.away_win_probability
  const favorite = homeFavored ? game.home.name : game.away.name
  const underdog = homeFavored ? game.away.name : game.home.name
  const rows: MarketVerdict[] = [{
    market: '승패',
    pick: `${favorite} 승`,
    probability: Math.max(p.home_win_probability, p.away_win_probability),
    actual: margin === 0 ? '무승부' : `${margin > 0 ? game.home.name : game.away.name} 승`,
    hit: margin === 0 ? null : (margin > 0) === homeFavored,
  }]
  const favoriteMinus = homeFavored ? p.handicap.home_minus_1_5 : p.handicap.away_minus_1_5
  const underdogPlus = homeFavored ? p.handicap.away_plus_1_5 : p.handicap.home_plus_1_5
  if (typeof favoriteMinus === 'number' && typeof underdogPlus === 'number') {
    const favoriteCovered = (homeFavored ? margin : -margin) >= 2
    const pickFavorite = favoriteMinus >= underdogPlus
    rows.push({
      market: '핸디캡 ±1.5',
      pick: pickFavorite ? `마핸 ${favorite} -1.5` : `플핸 ${underdog} +1.5`,
      probability: Math.max(favoriteMinus, underdogPlus),
      actual: favoriteCovered ? `${favorite} 2점차 이상 승` : `${underdog} 승 또는 1점차 패`,
      hit: pickFavorite === favoriteCovered,
    })
  }
  const totals = ranking.line != null ? p.totals[String(ranking.line)] : undefined
  if (ranking.line != null && totals) {
    const pickOver = totals.over >= totals.under
    rows.push({
      market: `총점 ${ranking.line} 기준`,
      pick: pickOver ? `오버 ${ranking.line}` : `언더 ${ranking.line}`,
      probability: Math.max(totals.over, totals.under),
      actual: total === ranking.line ? `기준점과 같은 ${total}점` : `${total > ranking.line ? '오버' : '언더'} (${total}점)`,
      hit: total === ranking.line ? null : (total > ranking.line) === pickOver,
    })
  }
  return rows
}

function rankedOutcomes(p: NonNullable<Game['prediction']>, game: Game): {
  outcomes: RankedOutcome[]
  line: number | null
  lineSource: '시장' | '모델'
} {
  const homeFavored = p.home_win_probability >= p.away_win_probability
  const favorite = homeFavored ? game.home.name : game.away.name
  const underdog = homeFavored ? game.away.name : game.home.name
  const outcomes: RankedOutcome[] = [
    { label: `${game.home.name} 승`, probability: p.home_win_probability },
    { label: `${game.away.name} 승`, probability: p.away_win_probability },
  ]
  const favoriteMinus = homeFavored ? p.handicap.home_minus_1_5 : p.handicap.away_minus_1_5
  const underdogPlus = homeFavored ? p.handicap.away_plus_1_5 : p.handicap.home_plus_1_5
  const marketSpread = game.market?.home_spread
  const marketFavorite = marketSpread == null || marketSpread === 0 ? null
    : marketSpread < 0 ? game.home.name : game.away.name
  const marketNote = marketFavorite == null ? ''
    : marketFavorite === favorite ? ' · 시장 마핸 일치' : ` · 시장 마핸은 ${marketFavorite}`
  if (typeof favoriteMinus === 'number') outcomes.push({
    label: `마핸 · ${favorite} -1.5`, probability: favoriteMinus,
    note: `${favorite}가 2점차 이상으로 이김${marketNote}`,
  })
  if (typeof underdogPlus === 'number') outcomes.push({
    label: `플핸 · ${underdog} +1.5`, probability: underdogPlus,
    note: `${underdog}가 이기거나 1점차로 짐${marketNote}`,
  })
  const marketLine = game.market?.total_line
  let line: number | null = marketLine != null && p.totals[String(marketLine)] ? marketLine : null
  let lineSource: '시장' | '모델' = '시장'
  if (line == null) {
    const target = p.total_quantiles?.p50 ?? p.statistical_expected_total ?? p.expected_total
    const half = Math.min(12.5, Math.max(6.5, Math.round(target - .5) + .5))
    if (p.totals[String(half)]) { line = half; lineSource = '모델' }
  }
  const totals = line != null ? p.totals[String(line)] : undefined
  if (line != null && totals) {
    const pushNote = totals.push ? ` · 정확히 ${line}점일 확률 ${pct(totals.push)}` : ''
    outcomes.push({ label: `오버 ${line}`, probability: totals.over, note: `두 팀 합계가 ${line}점보다 많음${pushNote}` })
    outcomes.push({ label: `언더 ${line}`, probability: totals.under, note: `두 팀 합계가 ${line}점보다 적음${pushNote}` })
  }
  if (game.league !== 'MLB' && p.tie_probability > 0) {
    outcomes.push({ label: '무승부', probability: p.tie_probability, note: '연장 11회까지 가도 동점' })
  }
  outcomes.sort((a, b) => b.probability - a.probability)
  return { outcomes, line, lineSource }
}

function modeFrequency(mode: { count?: number; probability?: number | null } | null | undefined) {
  if (!mode) return '횟수 정보 없음'
  if (mode.count == null) return mode.probability == null ? '횟수 정보 없음' : pct(mode.probability)
  return `${mode.count.toLocaleString()}번 나옴${mode.probability == null ? '' : ` · ${pct(mode.probability)}`}`
}

function outcomeLabel(outcome: 'HOME_WIN' | 'AWAY_WIN' | 'TIE', game: Game) {
  if (outcome === 'HOME_WIN') return `${game.home.name} 승`
  if (outcome === 'AWAY_WIN') return `${game.away.name} 승`
  return game.league === 'MLB' ? '동점' : '무승부'
}

function favoriteLabel(prediction: NonNullable<Game['prediction']>, game: Game) {
  return prediction.home_win_probability >= prediction.away_win_probability ? `${game.home.name} 승` : `${game.away.name} 승`
}

function shortDate(value: string) {
  const [, month, day] = value.split('-')
  return `${month}.${day}`
}
