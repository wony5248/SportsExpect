export type RecentWindow = {
  games: number
  wins: number
  draws: number
  win_rate: number
  avg_runs: number | null
  avg_runs_allowed: number | null
}

export type Team = {
  code: string
  name: string
  stats: null | {
    games: number
    wins: number
    losses: number
    draws: number
    win_rate: number
    runs_per_game: number | null
    runs_allowed_per_game: number | null
    ops: number | null
    era: number | null
    whip: number | null
    recent: Record<string, RecentWindow>
  }
  starter: null | {
    name: string | null
    confirmed: boolean
    era: number | null
    whip: number | null
    war: number | null
    games: number | null
    avg_start_innings: number | null
    quality_starts: number | null
    fip: number | null
    k_bb_rate: number | null
    rest_days: number | null
    recent_pitches: number | null
    handedness: string | null
    opponent_games: number | null
    opponent_innings: number | null
    opponent_era: number | null
    opponent_whip: number | null
  }
}

export type Prediction = {
  summary_schema_version?: number
  coherence_valid?: boolean
  probability_source?: string
  home_win_probability: number
  away_win_probability: number
  home_expected_runs: number
  away_expected_runs: number
  expected_total: number
  statistical_expected_total?: number
  display_expected_score?: { away: number; home: number }
  extra_innings?: { rule: string; probability: number }
  engine?: 'PLATE_APPEARANCE' | 'INNING_RATE'
  split_coverage?: { home: number; away: number } | null
  bullpen_usage?: Record<'home' | 'away', {
    starter_innings: number
    starter_share: number
    high_leverage_share: number
    middle_share: number
    chase_share: number
    mop_up_share: number
    multipliers: Record<'high_leverage' | 'middle' | 'chase' | 'mop_up', number>
  }>
  score_estimates?: {
    headline: 'MEAN'
    mean: { away: number; home: number }
    top5_weighted: null | { away: number; home: number; scores_used: number; coverage_probability: number }
    mode: { away: number; home: number; count?: number | null; probability?: number | null }
  }
  confidence: number
  confidence_label: 'HIGH' | 'MEDIUM' | 'LOW'
  confidence_missing: string[]
  classification_home_probability?: number
  simulation_home_probability?: number
  handicap: {
    home_minus_1_5: number
    away_plus_1_5: number
    away_minus_1_5?: number
    home_plus_1_5?: number
  }
  totals: Record<string, { over: number; under: number; push?: number }>
  tie_probability: number
  top_scores: SimulatedScore[]
  primary_score?: SimulatedScore
  simulation_modes?: {
    home_runs: SimulationMode<number>
    away_runs: SimulationMode<number>
    total_runs: SimulationMode<number>
    run_margin: SimulationMode<number>
    outcome: SimulationMode<'HOME_WIN' | 'AWAY_WIN' | 'TIE'>
  }
  total_quantiles?: { p10: number; p50: number; p90: number }
  team_dense_intervals?: {
    away: { low: number; high: number; mass: number }
    home: { low: number; high: number; mass: number }
  }
  total_dense_interval?: { low: number; high: number; mass: number }
  team_quantiles?: {
    away: { p10: number; p50: number; p90: number }
    home: { p10: number; p50: number; p90: number }
  }
  game_shape?: {
    one_run_probability: number
    blowout_probability: number
    either_shutout_probability: number
  }
  reasons: string[]
  model: { name: string; algorithm: string; simulations: number }
  ai_assist?: {
    enabled: boolean
    used: boolean
    status: string
    model: string | null
    blend_weight?: number
    reasons?: string[]
  }
  created_at: string
  disclaimer: string
}

export type SimulatedScore = {
  rank?: number
  home: number
  away: number
  count?: number
  probability: number | null
  trajectory_count?: number
  trajectory_probability_given_score?: number
  inning_line?: {
    inning: number
    away: number
    home: number
    away_cumulative: number
    home_cumulative: number
  }[]
}

export type SimulationMode<T> = {
  value: T
  count: number
  probability: number
}

export type Game = {
  id: string
  league: string
  date: string
  venue_date: string
  time: string | null
  start_at: string | null
  stadium: string | null
  status: string
  collected_at: string
  away: Team
  home: Team
  result: null | { away_score: number; home_score: number }
  prediction: Prediction | null
  market: null | {
    provider: string
    bookmaker_count: number
    total_line: number | null
    home_spread: number | null
    home_implied_probability: number | null
    away_implied_probability: number | null
    model_total_difference: number | null
    model_home_probability_difference: number | null
    source_url: string
    collected_at: string
  }
  prediction_history: {
    created_at: string
    home_win_probability: number
    away_win_probability: number
    home_expected_runs: number
    away_expected_runs: number
    confidence: number
    model: string
    stage: string | null
    changes: { type: string; label: string }[]
  }[]
  prediction_timeline: {
    captured_at: string
    stage: string
    trigger: string
    minutes_to_start: number | null
    changes: { type: string; label: string }[]
    home_win_probability: number
    away_win_probability: number
    home_expected_runs: number
    away_expected_runs: number
  }[]
  lineups: Record<'away' | 'home', {
    order: number
    player_id: string | null
    name: string
    position: string | null
    value: number | null
    value_metric: string | null
    confirmed: boolean
    opponent_pitcher_id: string | null
    matchup_plate_appearances: number | null
    matchup_at_bats: number | null
    matchup_hits: number | null
    matchup_doubles: number | null
    matchup_triples: number | null
    matchup_home_runs: number | null
    matchup_walks: number | null
    matchup_hit_by_pitch: number | null
    matchup_strikeouts: number | null
    matchup_avg: number | null
    matchup_obp: number | null
    matchup_slg: number | null
    matchup_ops: number | null
    collected_at: string
  }[]>
  sources: { name: string; url: string; collected_at: string }[]
  freshness: { last_updated_at: string; age_minutes: number; status: 'FRESH' | 'STALE' }
}

export type GameDate = {
  date: string
  games: number
  kbo: number
  mlb: number
}

export type OperationsStatus = {
  status: 'ok' | 'degraded'
  collection_data_stale: boolean
  failures_24h: number
  scheduled_games: number
  stored_predictions: number
  change_alerts_24h: number
  last_success: null | { collector: string; status: string; finished_at: string; error: string | null }
}

export type ClaudeKeyStatus = {
  configured: boolean
  enabled: boolean
  source: 'user' | 'none'
  fingerprint: string | null
  updated_at: string | null
  model: string
  error: string | null
  connection_verified?: boolean
  configured_model_available?: boolean
}

export type ClaudeModel = {
  id: string
  display_name: string
}

export type PersonalClaudeAnalysis = {
  game_id: string
  created_from_prediction_at: string
  model: string
  cached: boolean
  blend_weight: number
  baseline: {
    home_win_probability: number
    away_win_probability: number
    home_expected_runs: number
    away_expected_runs: number
  }
  personalized: {
    home_win_probability: number
    away_win_probability: number
    home_expected_runs: number
    away_expected_runs: number
    expected_total: number
    confidence: number
  }
  reasons: string[]
  caution: string
  usage?: { input_tokens: number; output_tokens: number }
  disclaimer: string
}

export type Backtest = {
  sample_size: number
  message?: string
  readiness?: {
    evaluable_pregame_games: number
    completed_results: number
    completed_without_evaluable_pregame_prediction: number
    preliminary_minimum: number
    recommended_minimum: number
    status: 'COLLECTING' | 'PRELIMINARY' | 'READY'
  }
  metrics?: { accuracy: number; brier_score: number; log_loss: number; calibration_error: number; runs_mae: number; runs_rmse: number }
  expanding_home_rate_baseline?: { brier_score: number; log_loss: number }
  model_leaderboard?: { model: string; sample_size: number; brier_score: number; log_loss: number }[]
}
