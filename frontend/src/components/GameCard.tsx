import { useState } from 'react'
import { Alert, Box, Button, Chip, CircularProgress, Collapse, Divider, LinearProgress, Stack, Typography } from '@mui/material'
import ExpandMoreRounded from '@mui/icons-material/ExpandMoreRounded'
import VerifiedRounded from '@mui/icons-material/VerifiedRounded'
import AutoAwesomeRounded from '@mui/icons-material/AutoAwesomeRounded'
import { fetchPersonalClaudeAnalysis } from '../lib/api'
import { getAccessToken } from '../lib/auth'
import type { Game, PersonalClaudeAnalysis, Prediction, Team } from '../types'

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
  const storedPrediction = game.prediction
  const replayPrediction = game.replay_prediction
  // Keep a genuine pre-game forecast as the primary record, but let a current, leakage-audited
  // replay provide the detailed archive card when that old record predates coherent summaries.
  const p = isDetailedPrediction(storedPrediction)
    ? storedPrediction
    : isDetailedPrediction(replayPrediction) ? replayPrediction : storedPrediction
  const coherent = isDetailedPrediction(p)
  // Forecasts saved before the engine field was introduced all used the inning-rate simulator.
  const engine = p?.engine ?? (coherent ? 'INNING_RATE' : undefined)
  // MLB games cannot end level, so a tied score from any stored payload is dropped before it
  // can be displayed. Older forecasts were saved before extra innings were simulated and do
  // contain them.
  const decidable = <T extends { home: number; away: number }>(scores: T[]) =>
    game.league === 'MLB' ? scores.filter((score) => score.home !== score.away) : scores
  const usableScores = decidable(p?.projected_score_candidates ?? p?.top_scores ?? [])
  const predictedScore = p ? ((p.summary_schema_version ?? 0) >= 10 && p.primary_score
    && !(game.league === 'MLB' && p.primary_score.home === p.primary_score.away)
    ? p.primary_score
    : selectRepresentativeScore(p, usableScores)) : undefined
  const displayedScoreCandidates = usableScores.length
    ? prioritizeScoreCandidates(usableScores, predictedScore)
    : []
  const expectedScore = predictedScore ? { away: predictedScore.away, home: predictedScore.home } : p?.display_expected_score ?? (p ? {
    away: Number(p.away_expected_runs.toFixed(1)), home: Number(p.home_expected_runs.toFixed(1)),
  } : undefined)
  const meanScore = p?.score_estimates?.mean ?? (p ? {
    away: Number(p.away_expected_runs.toFixed(1)), home: Number(p.home_expected_runs.toFixed(1)),
  } : undefined)
  const fullDistributionScore = p?.full_distribution_score
    ?? p?.score_estimates?.full_distribution
    ?? p?.score_estimates?.mode
  const closeGame = Boolean(p && Math.max(p.home_win_probability, p.away_win_probability) < .55)
  const scenarioBranches = p?.close_game_scenarios ?? p?.outcome_scores
  const awayWinScenario = scenarioBranches?.AWAY_WIN?.[0]
  const homeWinScenario = scenarioBranches?.HOME_WIN?.[0]
  const modeTotal = p?.simulation_modes?.total_runs
  const modeOutcome = p?.simulation_modes?.outcome
  const statisticalExpectedTotal = p?.statistical_expected_total ?? (
    typeof p?.home_expected_runs === 'number' && typeof p?.away_expected_runs === 'number'
      ? p.home_expected_runs + p.away_expected_runs
      : undefined
  )
  const isFinal = game.status === 'FINAL'
  const homeFavored = !p || p.home_win_probability >= p.away_win_probability
  // The home club bats last: it skips the ninth while ahead, so it can win more often while
  // averaging slightly fewer runs. Real, but it reads as a contradiction unless it is named.
  const battingLastGap = Boolean(meanScore && p && coherent
    && (meanScore.home > meanScore.away) !== (p.home_win_probability >= p.away_win_probability))
  const ranking = p && coherent ? rankedOutcomes(p, game, isFinal) : null
  const marginShape = p && coherent ? marginBuckets(p) : null
  const topOutcome = ranking?.outcomes[0]
  const handicap = p && coherent ? handicapSides(p, game) : null
  const picks = p && coherent && ranking && handicap ? marketPicks(p, game, ranking, handicap) : null
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
  const resultComparison = isFinal ? completedGameComparison(game, p, expectedScore) : null
  const isReplay = p?.origin === 'HISTORICAL_REPLAY'
  const evaluation = p?.evaluation
  const verdicts = isFinal && game.result && p && ranking ? marketVerdicts(p, game, ranking) : null
  const judgedVerdicts = verdicts?.filter((verdict) => verdict.hit != null) ?? []
  const verdictHits = judgedVerdicts.filter((verdict) => verdict.hit).length
  return (
    <article className="game-card">
      <Stack direction="row" justifyContent="space-between" alignItems="center" className="card-meta">
        <span>{game.league === 'MLB'
          ? `한국 ${shortDate(game.date)} ${game.time ?? '시간 미정'} KST · 미국 현지 ${shortDate(game.venue_date)} · ${game.stadium ?? '구장 미정'}`
          : `${game.time ?? '시간 미정'} KST · ${game.stadium ?? '구장 미정'}`}</span>
        <Stack direction="row" spacing={.7}>
          {engine && <Chip size="small"
            label={`${isReplay ? '과거 재현 · ' : ''}${engine === 'PLATE_APPEARANCE' ? '타석별' : '이닝별'}`}
            title={`${engine === 'PLATE_APPEARANCE' ? '타석별' : '이닝별'} 시뮬레이션 엔진`}
            className={`engine-chip${engine === 'PLATE_APPEARANCE' ? ' plate' : ''}${isReplay ? ' replay' : ''}`} />}
          <Chip size="small" label={game.freshness.status === 'FRESH' ? '최신' : '갱신 필요'} className={`freshness ${game.freshness.status.toLowerCase()}`} />
          <Chip size="small" label={gameStatusLabel(game.status)} className={`status ${game.status.toLowerCase()}`} />
        </Stack>
      </Stack>
      <Box className="matchup">
        <TeamName team={game.away} side="AWAY" />
        <Box className="versus"><span>VS</span><small>{game.league}</small></Box>
        <TeamName team={game.home} side="HOME" />
      </Box>
      {game.status === 'LIVE' && <Box className="live-game-notice" role="status">
        <b><i aria-hidden="true" />경기 중</b>
        <span>공식 경기 상태를 5분마다 확인합니다. 종료가 확정되면 실제 결과와 예측 적중 여부를 표시합니다.</span>
      </Box>}
      {isFinal && game.result && <Box className="result-comparison">
        <Stack direction="row" justifyContent="space-between" alignItems="baseline" className="result-comparison-heading">
          <b>{isReplay ? '과거 재현과 실제 결과 비교' : '경기 전 예측은 맞았을까'}{judgedVerdicts.length ? ` · ${judgedVerdicts.length}개 중 ${verdictHits}개 적중` : ''}</b>
          <span>{resultComparison
            ? isReplay
              ? `${new Date(p?.data_cutoff ?? resultComparison.createdAt).toLocaleString('ko-KR')} 이전 데이터만 사용`
              : `${new Date(resultComparison.createdAt).toLocaleString('ko-KR')}에 저장한 실전 예측`
            : '경기 전 저장한 예측 없음'}</span>
        </Stack>
        <Box className="result-comparison-grid">
          <Box className="result-score actual"><span>실제 최종</span><strong>{game.result.away_score} <i>:</i> {game.result.home_score}</strong><small>{game.away.name} : {game.home.name}</small></Box>
          {resultComparison ? <>
            <Box className="result-score predicted"><span>가장 확률 높은 점수</span><strong>{stat(resultComparison.awayExpected, 0)} <i>:</i> {stat(resultComparison.homeExpected, 0)}</strong><small>{game.away.name} : {game.home.name}{p?.extra_innings ? '' : ' · 9이닝만 계산한 이전 모델'}</small></Box>
            <Box className={`result-verdict ${resultComparison.verdictClass}`}><b>{resultComparison.verdict}</b><span>{resultComparison.favorite}</span><small>이 점수 기준 팀당 {stat(resultComparison.runsMae, 1)}점 차이</small></Box>
          </> : <Box className="result-verdict unavailable"><b>비교할 예측 없음</b><span>경기 시작 전에 저장해 둔 예측이 없습니다.</span></Box>}
        </Box>
        {evaluation && <SimulationEvaluation evaluation={evaluation} title={isReplay
          ? '과거 재현 · 실제 결과가 나온 횟수'
          : undefined} />}
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

        {!isFinal && picks && picks.length > 0 && <Box className="pick-strip">
          <span className="pick-strip-title">예측 흐름 · 승리팀 → 승리 시 점수차 → 총점</span>
          {picks.map((pick) => <Box key={pick.key}
            className={`pick${pick.hit == null ? '' : pick.hit ? ' hit' : ' miss'}`}>
            <span className="pick-market">{pick.market}{pick.line && <i>{pick.line}</i>}
              {pick.hit != null && <em>{pick.hit ? '적중' : '미적중'}</em>}</span>
            <strong>{pick.pick}</strong>
            {pick.probability != null && <><Box className="pick-gauge"><i style={{ width: pct(pick.probability) }} /></Box>
              <b>{pct(pick.probability)}</b></>}
            {pick.note && <small>{pick.note}</small>}
          </Box>)}
        </Box>}

        <Box className="score-row">
          <Box className="primary"><span>평균 점수 · 2만 회 전체</span><strong>{stat(meanScore?.away, 1)} <i>:</i> {stat(meanScore?.home, 1)}</strong><small>평균 총점 {stat(statisticalExpectedTotal, 1)}점{battingLastGap ? ` · ${game.home.name}는 앞서면 9회말을 치지 않아 평균이 낮게 나옵니다` : ''}</small></Box>
          <Divider orientation="vertical" flexItem />
          <Box><span>전체 분포에서 가장 잦은 결말</span><strong>{stat(fullDistributionScore?.away, 0)} <i>:</i> {stat(fullDistributionScore?.home, 0)}</strong><small>승패 조건 없이 2만 회 전체에서 최다{fullDistributionScore?.probability != null ? ` · ${pctFine(fullDistributionScore.probability)}` : ''}</small></Box>
        </Box>

        {closeGame && awayWinScenario && homeWinScenario ? <Box className="branch-scores">
          <span>승률 55% 미만 접전 · 한 점수로 단정하지 않고 양 팀 승리 시나리오를 함께 봅니다</span>
          <Box className="branch-grid">
            <Box className={`branch${!homeFavored ? ' likely' : ''}`}><span>{game.away.name}가 이길 때 대표</span><strong>{awayWinScenario.away} <i>:</i> {awayWinScenario.home}</strong><small>원정승 표본 안에서 {pctFine(awayWinScenario.probability_given_outcome)}</small></Box>
            <Box className={`branch${homeFavored ? ' likely' : ''}`}><span>{game.home.name}가 이길 때 대표</span><strong>{homeWinScenario.away} <i>:</i> {homeWinScenario.home}</strong><small>홈승 표본 안에서 {pctFine(homeWinScenario.probability_given_outcome)}</small></Box>
          </Box>
        </Box> : !closeGame && predictedScore ? <Box className="branch-scores">
          <span>예상 승리팀이 실제로 이긴다는 조건의 대표 점수</span>
          <Box className="branch-grid"><Box className="branch likely"><span>{homeFavored ? game.home.name : game.away.name} 승리 시나리오</span><strong>{expectedScore?.away} <i>:</i> {expectedScore?.home}</strong><small>{representativeSummary(predictedScore)}{p.extra_innings ? '' : ' · 9이닝만 계산한 이전 모델'}</small></Box></Box>
        </Box> : null}

        <Button fullWidth onClick={() => setOpen(!open)} endIcon={<ExpandMoreRounded className={open ? 'rotated' : ''} />} className="detail-button">{open ? '분석 접기' : '상세 분석 보기'}</Button>
        <Collapse in={open}>
          <Box className="details">
            <Typography variant="subtitle2">예측 상세</Typography>
        {displayedScoreCandidates.length ? <Box className="score-candidates">
          <span>위 예측과 같은 방향의 점수 후보</span>
          <Stack direction="row" flexWrap="wrap" gap={.8}>
            {displayedScoreCandidates.map((score, index) => <b key={`${score.away}-${score.home}`} className={index === 0 ? 'top' : ''}>
              <small className="rank">{index === 0 ? '선정' : '대안'}</small>{score.away} : {score.home}<small>{score.probability == null ? '—' : pctFine(score.probability)}</small>
            </b>)}
            {modeTotal ? <b className="mode-total">총점은 {modeTotal.value}점이 최다<small>{pctFine(modeTotal.probability)}</small></b> : null}
          </Stack>
        </Box> : null}

        {marginShape && <Box className="margin-shape">
          <Stack direction="row" justifyContent="space-between" alignItems="baseline">
            <span>어떤 경기가 될까</span>
            <small>{marginShape.headline}</small>
          </Stack>
          <Box className="margin-bar">
            {marginShape.buckets.map((bucket) => <i key={bucket.key} className={bucket.key}
              style={{ width: pct(bucket.share) }} title={`${bucket.label} ${pct(bucket.share)}`} />)}
          </Box>
          <Box className="margin-legend">
            {marginShape.buckets.map((bucket) => <span key={bucket.key} className={bucket.key}>
              {bucket.label} {pct(bucket.share)}
            </span>)}
          </Box>
        </Box>}

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
          <small className="ranking-note">상세 시장 확률은 전체 {p.model.simulations.toLocaleString()}회 기준 · 승패 / 핸디캡 -{handicap?.runLine ?? 1.5}/+{handicap?.runLine ?? 1.5} / 총점 {ranking.line != null
            ? ranking.lineSource === '시장'
              ? `${ranking.line} 기준 (실제 배당사 기준점)`
              : `${ranking.line} 기준 (배당 없어 우리가 계산)`
            : '기준점 없음'} · 위 예측 흐름의 점수차만 예측팀 승리 표본 안에서 다시 계산</small>
          {ranking.outcomes.map((outcome, index) => <Box key={outcome.label} className={`outcome-row${index === 0 ? ' top' : ''}`}>
            <i>{index + 1}</i>
            <Box className="outcome-label"><span>{outcome.label}</span>{outcome.note && <small>{outcome.note}</small>}</Box>
            <Box className="outcome-track"><b style={{ width: pct(outcome.probability) }} /></Box>
            <strong>{pct(outcome.probability)}</strong>
          </Box>)}
        </Box>}
            <Typography variant="subtitle2">선발 매치업</Typography>
            <Box className="starter-grid">
              <Starter team={game.away} /><Starter team={game.home} />
            </Box>
            {game.market && <>
              <Typography variant="subtitle2">배당사 기준점과 비교</Typography>
              <Box className="market-comparison">
                <Box><span>배당사 총점 기준</span><strong>{game.market.total_line ?? '—'}</strong><small>배당사 {game.market.bookmaker_count}곳의 중간값</small></Box>
                <Box><span>배당사 핸디캡 (마핸)</span><strong>{game.market.home_spread != null && game.market.home_spread !== 0
                  ? `${game.market.home_spread < 0 ? game.home.name : game.away.name} ${-Math.abs(game.market.home_spread)}`
                  : handicap?.fromMarket ? `${handicap.minusTeam} 우세`
                  : game.market.home_spread === 0 ? '핸디 없음' : '—'}</strong><small>{!handicap?.fromMarket
                  ? game.market.home_spread === 0 ? '배당사도 비슷하게 봄' : '아직 수집 전'
                  : `${game.market.home_spread == null ? '핸디 배당이 없어 승패 배당 기준 · ' : ''}${handicap.modelAgrees
                    ? '우리 예측과 같은 팀' : '우리 예측과 다른 팀'}`}</small></Box>
                <Box><span>홈 승률</span><strong>{pct(p.home_win_probability)} <i>/</i> {game.market.home_implied_probability == null ? '—' : pct(game.market.home_implied_probability)}</strong><small>우리 예측 / 배당사 (수수료 뺀 값)</small></Box>
                <Box><span>총점 차이</span><strong>{game.market.model_total_difference == null ? '—' : `${game.market.model_total_difference > 0 ? '+' : ''}${game.market.model_total_difference}`}</strong><small>우리 평균 총점이 배당사 기준보다 {game.market.model_total_difference == null ? '—' : game.market.model_total_difference > 0 ? '높음' : game.market.model_total_difference < 0 ? '낮음' : '같음'}</small></Box>
              </Box>
              <Typography className="source-note">{game.market.provider} · {new Date(game.market.collected_at).toLocaleString('ko-KR')} · 배당사 정보는 비교용으로만 보여드리며 베팅 추천이 아닙니다.</Typography>
            </>}
            {p.bullpen_usage && <>
              <Typography variant="subtitle2">투수 운영 예상</Typography>
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
            {p.pregame_context && Object.keys(p.pregame_context).length > 0 && <>
              <Typography variant="subtitle2">그날의 경기 조건</Typography>
              <Box className="market-comparison">
                <ContextWeather prediction={p} />
                {(['away', 'home'] as const).map((side) => <ContextTeam key={side} side={side} game={game} prediction={p} />)}
                <Box><span>데이터 원칙</span><strong>공식 데이터만</strong><small>공개 안 된 항목은 평균값으로 두고 신뢰도를 낮춥니다</small></Box>
              </Box>
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
            <Typography className="source-note">이 경기를 {p.model.simulations.toLocaleString()}번 가상으로 치러본 결과입니다. 팀 타격·수비, 선발과 불펜, 구장, 공개된 날씨·일정·라인업 요인은 모든 시행의 득점률에 반영됩니다. 타석 엔진은 타자별 득점권과 주자 상황 기록까지 매 타석에 적용합니다. 평균 점수, 대표 정수 점수, 승률, 마핸·플핸, 언더·오버는 모두 같은 {p.model.simulations.toLocaleString()}회 전체 분포에서 산출하며 미공개 자료만 중립값으로 둡니다. 평균 총점은 {stat(statisticalExpectedTotal, 2)}점입니다. {p.extra_innings
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

function SimulationMatch({ label, count, probability }: { label: string; count: number; probability: number }) {
  return <Box className="simulation-match"><small>{label}</small><b>{count.toLocaleString()}번</b><span>{pctFine(probability)}</span></Box>
}

function SimulationEvaluation({ evaluation, title }: {
  evaluation: NonNullable<NonNullable<Game['prediction']>['evaluation']>
  title?: string
}) {
  return <Box className="simulation-actual-match">
    <span className="simulation-match-title">{title ?? `실제 결과는 ${evaluation.simulation_count.toLocaleString()}번 중 얼마나 나왔나`}</span>
    <Box className="simulation-match-grid">
      <SimulationMatch label="동일 최종 점수" count={evaluation.actual_score_count} probability={evaluation.actual_score_probability} />
      <SimulationMatch label="동일 승패 결과" count={evaluation.actual_outcome_count} probability={evaluation.actual_outcome_probability} />
      <SimulationMatch label="동일 총점" count={evaluation.actual_total_count} probability={evaluation.actual_total_probability} />
      <SimulationMatch label="동일 점수차" count={evaluation.actual_margin_count} probability={evaluation.actual_margin_probability} />
      {evaluation.inning_data_available
        ? <SimulationMatch label="같은 이닝 흐름" count={evaluation.actual_inning_path_count ?? 0} probability={evaluation.actual_inning_path_probability ?? 0} />
        : <Box className="simulation-match unavailable"><small>같은 이닝 흐름</small><b>비교 대기</b><span>이닝별 기록이 있는 경기부터 표시</span></Box>}
    </Box>
  </Box>
}

function Starter({ team }: { team: Team }) {
  const p = team.starter
  return <Box><small>{team.name}</small><strong>{p?.name ?? '미정'}</strong><span>ERA {stat(p?.era)} · FIP {stat(p?.fip)} · WHIP {stat(p?.whip)}</span><span>{p?.recent?.available ? `최근 ${p.recent.starts ?? 0}선발 ERA ${stat(p.recent.era)} · WHIP ${stat(p.recent.whip)} · K-BB% ${stat((p.recent.k_bb_rate ?? 0) * 100, 1)}` : '최근 등판 세부 기록 없음'}</span><span>{p?.opponent_innings ? `오늘 상대팀 상대로 ${stat(p.opponent_innings, 1)}이닝 · ERA ${stat(p.opponent_era)} · WHIP ${stat(p.opponent_whip)}` : '오늘 상대팀과 맞붙은 기록 없음'}</span><span>{p?.rest_days != null ? `${p.rest_days}일 쉬고 등판` : '휴식일 정보 없음'}{p?.recent_pitches != null ? ` · 최근 5일 ${p.recent_pitches}구` : ''}{p?.recent?.derived_pitch_limit ? ` · 최근 투구수 기반 ${p.recent.derived_pitch_limit}구 범위` : ''}</span></Box>
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
    {entries.length ? <ol>{entries.map((entry) => <li key={`${entry.order}-${entry.name}`}><span>{entry.name}</span><small>{[
      entry.position ?? '—',
      entry.platoon_plate_appearances ? `${entry.platoon_opponent_hand === 'L' ? '좌' : '우'}투수 상대 OPS ${stat(entry.platoon_ops, 3)} (${entry.platoon_plate_appearances}타석)` : null,
      entry.matchup_plate_appearances ? `오늘 선발 상대 OPS ${stat(entry.matchup_ops, 3)} (${entry.matchup_plate_appearances}타석)` : null,
    ].filter(Boolean).join(' · ')}</small></li>)}</ol> : <p>발표 전</p>}
  </Box>
}

function ContextWeather({ prediction }: { prediction: Prediction }) {
  const weather = prediction.pregame_context?.weather
  return <Box><span>날씨·구장</span><strong>{weather?.available
    ? `${weather.temperature_f ?? '—'}℉ · ${weather.condition ?? '정보 있음'}` : '경기 시각 정보 없음'}</strong>
    <small>{weather?.available ? `${weather.wind ?? '바람 정보 없음'} · 득점환경 ×${stat(weather.run_multiplier, 3)}` : '정적 구장 계수만 반영'}</small></Box>
}

function ContextTeam({ side, game, prediction }: { side: 'away' | 'home'; game: Game; prediction: Prediction }) {
  const bullpen = prediction.pregame_context?.bullpen?.[side]
  const schedule = prediction.pregame_context?.schedule?.[side]
  const yesterday = bullpen?.pitches?.['1']
  return <Box><span>{game[side].name} 피로도</span><strong>불펜 {bullpen?.available ? pct(bullpen.fatigue_index ?? 0) : '미확보'} · 일정 {pct(schedule?.fatigue_index ?? 0)}</strong>
    <small>{bullpen?.available ? `전날 구원 ${yesterday ?? 0}구${bullpen.high_load_arms?.length ? ` · 고부하 ${bullpen.high_load_arms.length}명` : ''}` : '공식 구원 등판 기록 없음'} · 최근 3일 {schedule?.games_last_3d ?? 0}경기{schedule?.travel_km != null ? ` · 이동 ${Math.round(schedule.travel_km)}km` : ''}</small></Box>
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
  return '당시 저장된 경기 전 예측이 없습니다. 경기 전 데이터만 쓴 과거 재현이 만들어지면 실제 결과와 비교해 표시합니다.'
}

function gameStatusLabel(status: string) {
  return ({ SCHEDULED: '경기 예정', LIVE: '경기 중', FINAL: '경기 종료', CANCELLED: '경기 취소' } as Record<string, string>)[status] ?? status
}

function isDetailedPrediction(prediction: Prediction | null): prediction is Prediction {
  return Boolean(prediction && (prediction.summary_schema_version ?? 0) >= 2 && prediction.coherence_valid === true)
}

function completedGameComparison(game: Game, prediction: Prediction | null,
                                 expectedScore: { away: number; home: number } | undefined) {
  if (game.status !== 'FINAL') return null
  const result = game.result
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

type RankedOutcome = { label: string; probability: number; note?: string; hit?: boolean; actual?: string }

type MarketVerdict = { market: string; pick: string; probability: number; actual: string; hit: boolean | null }

// 마핸/플핸 is a market label: the team laying the runs is whoever the book made favorite,
// not whoever our simulation happens to like. KBO books rarely publish a run line, so fall back
// to the moneyline gap, and only to our own model when the market says nothing at all.
/** The three markets a reader actually came to check, each reduced to: what is the line, which
 *  side does the simulation favour, and by how much. Everything else on the card is supporting
 *  detail for these three answers. */
function marketPicks(p: NonNullable<Game['prediction']>, game: Game,
                     ranking: ReturnType<typeof rankedOutcomes>,
                     handicap: ReturnType<typeof handicapSides>) {
  const picks: {
    key: string; market: string; line: string; pick: string; probability?: number
    note?: string; hit?: boolean
  }[] = []

  const result = game.status === 'FINAL' ? game.result : null
  const margin = result ? result.home_score - result.away_score : null
  const actualTotal = result ? result.home_score + result.away_score : null

  const modelHomeFavored = p.home_win_probability >= p.away_win_probability
  const modelFavorite = modelHomeFavored ? game.home.name : game.away.name
  const favoriteWinProbability = modelHomeFavored ? p.home_win_probability : p.away_win_probability
  picks.push({
    key: 'winner', market: '승패', line: '', pick: `${modelFavorite} 승`,
    probability: favoriteWinProbability,
    hit: margin == null ? undefined : (margin > 0) === modelHomeFavored,
    note: '전체 2만 회 기준',
  })

  const score = p.primary_score
  const coverGivenWin = score?.favorite_cover_probability_given_win
  if (handicap.fromMarket && !handicap.modelAgrees) {
    picks.push({
      key: 'handicap',
      market: '플핸 · 승리 포함',
      line: `+${handicap.runLine}`,
      pick: `${modelFavorite} 플핸`,
      probability: handicap.plusProbability,
      hit: margin == null ? undefined : handicap.homeMinus
        ? margin < handicap.runLine
        : -margin < handicap.runLine,
      note: `${modelFavorite} 승리 또는 ${handicap.minimumMargin - 1}점차 이내 패배를 모두 포함`,
    })
  } else if (score && coverGivenWin != null) {
    const runLine = score.favorite_run_line ?? handicap.runLine
    const minimumMargin = score.minimum_favorite_margin ?? Math.floor(runLine) + 1
    const projectsCover = score.projects_favorite_cover === true
    const actualFavoriteMargin = margin == null ? null : modelHomeFavored ? margin : -margin
    picks.push({
      key: 'handicap',
      market: projectsCover ? '승리 시 · 마핸' : '승리 시 · 보수 분기',
      line: projectsCover ? `-${runLine}` : `<${minimumMargin}점차`,
      pick: projectsCover ? `${modelFavorite} ${minimumMargin}점차+` : `${modelFavorite} ${minimumMargin - 1}점차 이내`,
      probability: projectsCover ? coverGivenWin : undefined,
      hit: actualFavoriteMargin == null ? undefined : projectsCover
        ? actualFavoriteMargin >= minimumMargin
        : actualFavoriteMargin >= 1 && actualFavoriteMargin < minimumMargin,
      note: projectsCover
        ? `${modelFavorite}가 이긴 표본 중 · 전체 승률 ${pct(favoriteWinProbability)}`
        : `승리 시 ${minimumMargin}점차+ ${pct(coverGivenWin)} · 대표점수 기준 72% 미만`,
    })
  }

  const totals = ranking.line != null ? p.totals[String(ranking.line)] : undefined
  if (ranking.line != null && totals) {
    const takeOver = totals.over >= totals.under
    picks.push({
      key: 'total',
      market: '총점',
      line: String(ranking.line),
      pick: takeOver ? '오버' : '언더',
      probability: takeOver ? totals.over : totals.under,
      hit: actualTotal == null || actualTotal === ranking.line ? undefined
        : takeOver ? actualTotal > ranking.line : actualTotal < ranking.line,
      note: ranking.lineSource === '시장' ? '실제 배당 기준점' : '배당 없어 우리가 계산',
    })
  }
  return picks
}

function handicapSides(p: NonNullable<Game['prediction']>, game: Game) {
  const spread = game.market?.home_spread
  const pricedMarket = p.market_handicap
  const hasPricedMarket = spread != null && pricedMarket != null
    && Math.abs(pricedMarket.home_spread - spread) < .001
  const homeImplied = game.market?.home_implied_probability
  const awayImplied = game.market?.away_implied_probability
  const impliedGap = homeImplied != null && awayImplied != null ? homeImplied - awayImplied : null
  const marketHomeMinus = spread != null && spread !== 0 ? spread < 0
    : impliedGap != null && Math.abs(impliedGap) >= .01 ? impliedGap > 0
    : null
  const modelHomeFavored = p.home_win_probability >= p.away_win_probability
  const homeMinus = marketHomeMinus ?? modelHomeFavored
  const runLine = hasPricedMarket ? pricedMarket.run_line : 1.5
  return {
    homeMinus,
    runLine,
    minimumMargin: hasPricedMarket ? pricedMarket.minimum_margin : 2,
    minusTeam: homeMinus ? game.home.name : game.away.name,
    plusTeam: homeMinus ? game.away.name : game.home.name,
    minusProbability: hasPricedMarket ? pricedMarket.minus_probability
      : homeMinus ? p.handicap.home_minus_1_5 : p.handicap.away_minus_1_5,
    plusProbability: hasPricedMarket ? pricedMarket.plus_probability
      : homeMinus ? p.handicap.away_plus_1_5 : p.handicap.home_plus_1_5,
    pushProbability: hasPricedMarket ? pricedMarket.push_probability : 0,
    fromMarket: marketHomeMinus != null,
    modelAgrees: homeMinus === modelHomeFavored,
    modelFavorite: modelHomeFavored ? game.home.name : game.away.name,
  }
}

function marketVerdicts(p: NonNullable<Game['prediction']>, game: Game,
                        ranking: ReturnType<typeof rankedOutcomes>): MarketVerdict[] {
  if (game.status !== 'FINAL') return []
  const result = game.result
  if (!result) return []
  const margin = result.home_score - result.away_score
  const total = result.home_score + result.away_score
  const homeFavored = p.home_win_probability >= p.away_win_probability
  const favorite = homeFavored ? game.home.name : game.away.name
  const rows: MarketVerdict[] = [{
    market: '승패',
    pick: `${favorite} 승`,
    probability: Math.max(p.home_win_probability, p.away_win_probability),
    actual: margin === 0 ? '무승부' : `${margin > 0 ? game.home.name : game.away.name} 승`,
    hit: margin === 0 ? null : (margin > 0) === homeFavored,
  }]
  const handicap = handicapSides(p, game)
  if (typeof handicap.minusProbability === 'number' && typeof handicap.plusProbability === 'number') {
    const favoriteMargin = handicap.homeMinus ? margin : -margin
    const minusCovered = favoriteMargin > handicap.runLine
    const pushed = favoriteMargin === handicap.runLine
    const pickMinus = handicap.minusProbability >= handicap.plusProbability
    rows.push({
      market: `핸디캡 -${handicap.runLine}/+${handicap.runLine}`,
      pick: pickMinus ? `마핸 ${handicap.minusTeam} -${handicap.runLine}` : `플핸 ${handicap.plusTeam} +${handicap.runLine}`,
      probability: Math.max(handicap.minusProbability, handicap.plusProbability),
      actual: pushed ? `${handicap.runLine}점차 · 적특` : minusCovered
        ? `${handicap.minusTeam} ${handicap.minimumMargin}점차 이상 승`
        : `${handicap.plusTeam} 핸디캡 승`,
      hit: pushed ? null : pickMinus === minusCovered,
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

function rankedOutcomes(p: NonNullable<Game['prediction']>, game: Game, includeResult = game.status === 'FINAL'): {
  outcomes: RankedOutcome[]
  line: number | null
  lineSource: '시장' | '모델'
} {
  const result = includeResult ? game.result : null
  const actual = result ? `실제 ${result.away_score} : ${result.home_score}` : undefined
  const margin = result ? result.home_score - result.away_score : null
  const outcomes: RankedOutcome[] = [
    { label: `${game.home.name} 승`, probability: p.home_win_probability,
      hit: margin == null ? undefined : margin > 0, actual },
    { label: `${game.away.name} 승`, probability: p.away_win_probability,
      hit: margin == null ? undefined : margin < 0, actual },
  ]
  const handicap = handicapSides(p, game)
  const handicapNote = !handicap.fromMarket ? ' · 핸디 배당이 없어 우리 예측 기준'
    : handicap.modelAgrees ? ' · 배당사가 꼽은 팀'
    : ` · 배당사 마핸이지만 우리는 ${handicap.modelFavorite} 우세로 봄`
  if (typeof handicap.minusProbability === 'number') outcomes.push({
    label: `마핸 · ${handicap.minusTeam} -${handicap.runLine}`, probability: handicap.minusProbability,
    note: `${handicap.minusTeam}가 ${handicap.minimumMargin}점차 이상으로 이김${handicapNote}`,
    hit: margin == null || (handicap.homeMinus ? margin : -margin) === handicap.runLine ? undefined
      : (handicap.homeMinus ? margin : -margin) > handicap.runLine, actual,
  })
  if (typeof handicap.plusProbability === 'number') outcomes.push({
    label: `플핸 · ${handicap.plusTeam} +${handicap.runLine}`, probability: handicap.plusProbability,
    note: `${handicap.plusTeam}가 이기거나 ${handicap.minimumMargin - 1}점차 이내로 짐${handicapNote}`,
    hit: margin == null || (handicap.homeMinus ? margin : -margin) === handicap.runLine ? undefined
      : (handicap.homeMinus ? margin : -margin) < handicap.runLine, actual,
  })
  // The total line is always the Odds API's real market number when we have collected one for
  // this game; the simulation only ever answers "over or under THAT line". A model-derived line
  // is a fallback for the rare game with no market data, never a substitute for a real one.
  const marketLine = game.market?.total_line
  let line: number | null = marketLine != null && p.totals[String(marketLine)] ? marketLine : null
  let lineSource: '시장' | '모델' = '시장'
  if (line == null) {
    // No market line for this game (odds not collected, or the book's line falls outside our
    // supported range). Anchoring at the median plus a flat half run guaranteed "under" won
    // every time here too - under was then just the probability of landing at or below the
    // median. Use the line the model itself treats as even money instead.
    line = fairTotalLine(p)
    lineSource = '모델'
  }
  const totals = line != null ? p.totals[String(line)] : undefined
  if (line != null && totals) {
    const pushNote = totals.push ? ` · 정확히 ${line}점일 확률 ${pct(totals.push)}` : ''
    const actualTotal = result ? result.home_score + result.away_score : null
    outcomes.push({ label: `오버 ${line}`, probability: totals.over, note: `두 팀 합계가 ${line}점보다 많음${pushNote}`,
      hit: actualTotal == null ? undefined : actualTotal > line, actual })
    outcomes.push({ label: `언더 ${line}`, probability: totals.under, note: `두 팀 합계가 ${line}점보다 적음${pushNote}`,
      hit: actualTotal == null ? undefined : actualTotal < line, actual })
  }
  if (game.league !== 'MLB' && p.tie_probability > 0) {
    outcomes.push({ label: '무승부', probability: p.tie_probability, note: '연장 11회까지 가도 동점',
      hit: margin == null ? undefined : margin === 0, actual })
  }
  outcomes.sort((a, b) => b.probability - a.probability)
  return { outcomes, line, lineSource }
}

/** The half-run total the model itself treats as even money, which is what a book would post. */
function fairTotalLine(p: NonNullable<Game['prediction']>) {
  const lines = Object.keys(p.totals)
    .map(Number)
    .filter((line) => Number.isFinite(line) && !Number.isInteger(line))
  if (!lines.length) return null
  return lines.reduce((best, line) => {
    const gap = (value: number) => Math.abs(p.totals[String(value)].over - p.totals[String(value)].under)
    return gap(line) < gap(best) ? line : best
  })
}

/** How the game is likely to feel, which separates matchups far better than an exact score.
 *  Across a single slate the one-run-or-two share ranges from 43% to 59% and the blowout share
 *  from 17% to 36%, while the most likely exact score is 2-3 or 3-4 in almost every game. */
function marginBuckets(p: NonNullable<Game['prediction']>) {
  const margins = p.frequency_tables?.margins
  if (!margins) return null
  const runs = Object.entries(margins)
  const total = runs.reduce((sum, [, count]) => sum + count, 0)
  if (!total) return null
  const share = (test: (margin: number) => boolean) =>
    runs.filter(([margin]) => test(Math.abs(Number(margin)))).reduce((sum, [, count]) => sum + count, 0) / total
  const buckets = [
    { key: 'close', label: '접전 1~2점차', share: share((margin) => margin >= 1 && margin <= 2) },
    { key: 'clear', label: '3~4점차', share: share((margin) => margin >= 3 && margin <= 4) },
    { key: 'blowout', label: '5점차 이상', share: share((margin) => margin >= 5) },
  ]
  const level = share((margin) => margin === 0)
  if (level > 0) buckets.push({ key: 'level', label: '무승부', share: level })
  const leading = buckets.reduce((best, bucket) => bucket.share > best.share ? bucket : best)
  return { buckets, headline: `${leading.label} 가능성이 가장 큼` }
}

function selectRepresentativeScore(
  p: NonNullable<Game['prediction']>,
  candidates: NonNullable<Game['prediction']>['top_scores'] = p.top_scores,
) {
  if (!candidates.length) return p.primary_score
  const evidence = (score: NonNullable<Game['prediction']>['top_scores'][number]) =>
    score.probability ?? score.count ?? 0
  const maxEvidence = Math.max(...candidates.map(evidence)) || 1
  const expectedTotal = p.home_expected_runs + p.away_expected_runs
  const favoriteStrength = Math.min(1, Math.abs(p.home_win_probability - .5) / .15)
  const favoredHome = p.home_win_probability >= .5
  const fit = (score: NonNullable<Game['prediction']>['top_scores'][number]) => {
    const teamError = Math.abs(score.home - p.home_expected_runs) + Math.abs(score.away - p.away_expected_runs)
    const frequencyFit = evidence(score) / maxEvidence
    const teamFit = 1 / (1 + teamError / 2)
    const totalFit = 1 / (1 + Math.abs(score.home + score.away - expectedTotal) / 2)
    const directionMatches = score.home === score.away ? null : (score.home > score.away) === favoredHome
    const rawDirectionFit = directionMatches == null ? .4 : directionMatches ? 1 : 0
    const directionFit = (1 - favoriteStrength) * .5 + favoriteStrength * rawDirectionFit
    return .50 * frequencyFit + .25 * teamFit + .15 * totalFit + .10 * directionFit
  }
  return candidates.reduce((best, score) => fit(score) > fit(best) ? score : best)
}

function prioritizeScoreCandidates(
  scores: NonNullable<Game['prediction']>['top_scores'],
  selected: NonNullable<Game['prediction']>['top_scores'][number] | undefined,
) {
  if (!selected) return scores.slice(0, 3)
  return [selected, ...scores.filter((score) => score.away !== selected.away || score.home !== selected.home)].slice(0, 3)
}

function modeFrequency(mode: { count?: number; probability?: number | null } | null | undefined) {
  if (!mode) return '횟수 정보 없음'
  if (mode.count == null) return mode.probability == null ? '횟수 정보 없음' : pct(mode.probability)
  return `${mode.count.toLocaleString()}번 나옴${mode.probability == null ? '' : ` · ${pct(mode.probability)}`}`
}

function representativeSummary(score: NonNullable<Game['prediction']>['primary_score'] | undefined) {
  if (!score) return '대표 점수 없음'
  if (score.selection_method !== 'COHERENT_BAYES_MEDIAN_V3') return modeFrequency(score)
  // The selection diagnostics (conditioning mode, cover probability, matched share) belong in
  // the model notes, not on the face of the card. A reader only needs to know that this score
  // was picked from the simulations that agree with the picks shown above it.
  const parts: string[] = ['위 예측과 같은 방향의 시뮬레이션에서 고른 점수']
  if (score.scenario_probability != null) parts.push(`해당 시뮬레이션 ${pct(score.scenario_probability)}`)
  return parts.join(' · ')
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
