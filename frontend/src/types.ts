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
    recent?: { available?: boolean; starts?: number; era?: number | null; whip?: number | null; k_bb_rate?: number | null; avg_pitches?: number | null; derived_pitch_limit?: number | null; reason?: string }
    handedness: string | null
    opponent_games: number | null
    opponent_innings: number | null
    opponent_era: number | null
    opponent_whip: number | null
  }
}

export type Prediction = {
  origin?: 'LIVE_PREGAME' | 'HISTORICAL_REPLAY'
  data_cutoff?: string | null
  training_eligible?: boolean
  leakage_audit?: { passed?: boolean; method?: string; note?: string; [key: string]: unknown }
  features?: {
    home_starter_confirmed?: boolean
    away_starter_confirmed?: boolean
    home_lineup_confirmed?: boolean
    away_lineup_confirmed?: boolean
  }
  evaluation?: null | {
    simulation_count: number
    actual_score_count: number
    actual_score_probability: number
    actual_outcome_count: number
    actual_outcome_probability: number
    actual_total_count: number
    actual_total_probability: number
    actual_margin_count: number
    actual_margin_probability: number
    actual_inning_path_count: number | null
    actual_inning_path_probability: number | null
    inning_data_available: boolean
  }
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
  pregame_context?: {
    weather?: { available?: boolean; temperature_f?: number | null; condition?: string; wind?: string; run_multiplier?: number; reason?: string }
    bullpen?: Record<'home' | 'away', { available?: boolean; fatigue_index?: number; pitches?: Record<string, number>; high_load_arms?: string[]; confirmed_unavailable_arms?: string[]; reason?: string }>
    schedule?: Record<'home' | 'away', { games_last_3d?: number; consecutive_days?: number; travel_km?: number | null; fatigue_index?: number }>
    availability?: { weather?: boolean; bullpen?: boolean; schedule?: boolean }
  }
  residual_calibration?: {
    enabled: boolean
    policy_version?: number
    league_residual_sd?: number
    home_variance_multiplier?: number
    away_variance_multiplier?: number
    source_game_count?: number
    method?: string
    home?: ResidualTeamProjection
    away?: ResidualTeamProjection
    outlier_analysis?: Partial<Record<'home_scoring' | 'away_scoring', {
      combined_outlier_index?: number
      large_residual_flag?: boolean
      offense_large_residual_team?: boolean
      opponent_defense_large_residual_team?: boolean
      matchup_residual_flag?: boolean
      matchup_games?: number
      matchup_direction?: string
      matchup_direction_consistency?: number
    }>>
  }
  market_calibration?: {
    enabled: boolean
    method?: string
    reason?: string
    provider?: string | null
    collected_at?: string | null
    bookmaker_count?: number
    total_line?: number | null
    home_spread?: number | null
    market_home_probability?: number | null
    market_probability_source?: string | null
    total_weight?: number
    probability_weight?: number
    model_home_before?: number
    model_away_before?: number
    model_total_before?: number
    model_home_probability_before?: number
    anchored_home_probability?: number
    anchored_home?: number
    anchored_away?: number
    anchored_total?: number
  }
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
    mode: { away: number; home: number; count?: number | null; probability?: number | null }
    full_distribution?: { away: number; home: number; count?: number | null; probability?: number | null }
    representative?: SimulatedScore
    winner_conditional?: SimulatedScore
  }
  confidence: number
  confidence_label: 'HIGH' | 'MEDIUM' | 'LOW'
  confidence_missing: string[]
  classification_home_probability?: number
  simulation_home_probability?: number
  raw_simulation_home_probability?: number
  raw_simulation_away_probability?: number
  probability_calibration?: {
    enabled: boolean
    method?: string
    sample_count?: number
    minimum_samples?: number
    slope?: number
    intercept?: number
    data_cutoff?: string
    reason?: string
    raw_home_two_way_probability?: number
    raw_away_two_way_probability?: number
    target_home_two_way_probability?: number
    target_away_two_way_probability?: number
    raw_branch_counts?: Record<'home_win' | 'away_win' | 'tie', number>
    reweighted_branch_counts?: Record<'home_win' | 'away_win' | 'tie', number>
    population_size?: number
  }
  handicap: {
    home_minus_1_5: number
    away_plus_1_5: number
    away_minus_1_5?: number
    home_plus_1_5?: number
  }
  market_handicap?: {
    home_spread: number
    run_line: number
    minimum_margin: number
    minus_side: 'HOME' | 'AWAY'
    minus_probability: number
    plus_probability: number
    push_probability: number
  } | null
  totals: Record<string, { over: number; under: number; push?: number }>
  /** Second stage. The run line and the total priced inside only the simulations the forecast
   *  winner actually wins. `*_probability` fields are conditional on that branch happening;
   *  `joint_*` fields also include the chance the branch happens at all, which is what a
   *  reader is really risking. Absent on forecasts saved before schema 28, and null when the
   *  winning branch held too few simulations to price. */
  winner_conditional_market?: WinnerConditionalMarket | null
  /** Why this game is more or less predictable than the league baseline. Upsets come from a
   *  wide distribution, not from a better underdog, so these widen the spread. */
  upset_volatility?: {
    shared_volatility: number
    home_volatility: number
    away_volatility: number
    maximum_bonus: number
    detail?: Record<string, unknown>
  }
  /** The club the market made underdog, our probability for it, and the posted price. Flagged
   *  only when we beat that price by more than the threshold — never on confidence alone. */
  upset_watch?: {
    underdog: 'HOME' | 'AWAY'
    underdog_source: 'MARKET' | 'MODEL'
    model_probability: number
    market_probability: number | null
    edge: number | null
    edge_threshold: number
    flagged: boolean
    comparable: boolean
    volatility_bonus: number
    reasons: string[]
    validation: string
  }
  tie_probability: number
  top_scores: SimulatedScore[]
  full_distribution_score?: SimulatedScore
  winner_conditional_score?: SimulatedScore
  projected_score_candidates?: SimulatedScore[]
  frequency_tables?: {
    scores?: Record<string, number>
    totals?: Record<string, number>
    margins?: Record<string, number>
    outcomes?: Record<string, number>
  }
  outcome_scores?: Partial<Record<'HOME_WIN' | 'AWAY_WIN' | 'TIE', {
    home: number
    away: number
    count: number
    probability_given_outcome: number
  }[]>>
  team_run_distribution?: Record<'home' | 'away', number[]>
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

export type ResidualTeamProjection = {
  games?: number
  offense?: number
  defense?: number
  offense_residual_mae?: number
  defense_residual_mae?: number
  offense_large_residual_games?: number
  defense_large_residual_games?: number
  offense_large_residual_share?: number
  defense_large_residual_share?: number
  offense_outlier_index?: number
  defense_outlier_index?: number
  offense_large_residual_team?: boolean
  defense_large_residual_team?: boolean
  matchup?: number
  matchup_games?: number
  matchup_residual_mean_raw?: number
  matchup_residual_mae?: number
  matchup_large_residual_games?: number
  matchup_large_residual_share?: number
  matchup_direction_consistency?: number
  matchup_residual_direction?: string
  matchup_residual_flag?: boolean
}

export type WinnerConditionalMarket = {
  winner: 'HOME' | 'AWAY'
  winner_probability: number
  /** Share of all simulations inside the branch. Below `winner_probability` in a league that
   *  allows ties, because a tie belongs to neither club's branch. */
  scenario_probability: number
  sample_size: number
  population_size: number
  conditioning: 'WINNER_WINS_OUTRIGHT'
  mean_runs: { home: number; away: number }
  median_runs: { home: number; away: number }
  median_total: number
  median_margin: number
  handicap: {
    run_line: number
    /** A run line is one two-sided quote, so the posted magnitude prices both clubs and stays
     *  the reference whichever club the book made favourite. MODEL_FALLBACK only when no spread
     *  was collected for this game at all. */
    run_line_source: 'MARKET' | 'MODEL_FALLBACK'
    market_home_spread: number | null
    /** Whether the book laid the runs on the same club the model made favourite. */
    market_agrees_with_model: boolean
    /** The club laying the runs per the posted spread — the side the market's price refers to. */
    minus_side: 'HOME' | 'AWAY'
    plus_side: 'HOME' | 'AWAY'
    minimum_margin: number
    /** Our probability for the book's own event, over the whole population, two-way. */
    model_minus_probability: number
    model_plus_probability: number
    /** The book's de-vigged price for the same event, null when no run-line price was collected. */
    market_minus_probability: number | null
    market_plus_probability: number | null
    /** model_minus - market_minus. Positive means we give the club laying the runs a better
     *  chance than the book does. Null without a collected price. */
    edge: number | null
    pick: 'MINUS' | 'PLUS'
    pick_probability: number
    pick_edge: number | null
    /** EDGE_VS_MARKET is a real recommendation. NO_MARKET_PRICE means no run-line price was
     *  collected, so the card is showing the branch narrative rather than a comparison. */
    pick_basis: 'EDGE_VS_MARKET' | 'NO_MARKET_PRICE'
    comparable: boolean
    /** How the forecast winner gets there, conditional on it winning. Evidence, not the
     *  decision: a winning club clears a 1.5 line in nearly every matchup. */
    winner_side: 'HOME' | 'AWAY'
    winner_cover_probability: number
    winner_short_probability: number
    winner_push_probability: number
    joint_winner_cover_probability: number
  }
  headline_total: {
    line: number
    line_source: 'MARKET' | 'MODEL_FAIR'
    /** Conditional on the forecast winner winning: how the total looks inside that branch. */
    over_probability: number
    under_probability: number
    push_probability: number
    /** Our probability for the book's own event, over the whole population, two-way. */
    model_over_probability: number
    model_under_probability: number
    /** The book's de-vigged price at the same line, null when none was collected for it. */
    market_over_probability: number | null
    market_under_probability: number | null
    edge: number | null
    /** Total of the representative headline score used to choose over/under. */
    expected_total?: number
    pick: 'OVER' | 'UNDER' | 'PUSH'
    pick_probability: number
    pick_edge: number | null
    pick_basis: 'HEADLINE_SCORE_TOTAL_VS_LINE' | 'EXPECTED_TOTAL_VS_LINE' | 'EDGE_VS_MARKET' | 'NO_MARKET_PRICE'
    comparable: boolean
    joint_over_probability: number
    joint_under_probability: number
    joint_pick_probability: number
  }
  totals: Record<string, { over: number; under: number; push: number }>
  favorite_run_line: number
  minimum_favorite_margin: number
  favorite_cover_probability: number
  projects_favorite_cover: boolean
  /** True when the displayed pick is what set the headline score's margin. */
  headline_follows_pick: boolean
  margin_probabilities: Record<string, number>
  top_scores: { home: number; away: number; count: number; probability_given_winner: number }[]
}

export type SimulatedScore = {
  rank?: number
  home: number
  away: number
  count?: number
  probability: number | null
  selection_method?: string
  selection_score?: number | null
  population_coverage?: number
  projects_favorite_cover?: boolean
  run_line_conditioning?: 'WINNER_CONDITIONAL_COVER_MAJORITY' | 'UNCONDITIONAL_COVER_MAJORITY'
    | 'WINNER_CONDITIONAL_COVER_SIGNAL' | 'RUN_LINE_CONSERVATIVE'
  favorite_cover_probability?: number
  favorite_cover_probability_given_win?: number
  favorite_run_line?: number
  minimum_favorite_margin?: number
  run_line_source?: 'MARKET' | 'MODEL_FALLBACK'
  target_population?: 'WINNER_BRANCH' | 'FULL_POPULATION'
  headline_total_line?: number
  headline_total_pick?: 'OVER' | 'UNDER'
  total_conditioning?: 'MARKET_EDGE' | 'WINNER_CONDITIONAL' | 'FULL_POPULATION'
  /** Full-population reading of the same line, kept as the reference. */
  headline_over_probability?: number
  headline_under_probability?: number
  headline_push_probability?: number
  /** The same two numbers inside the winning branch, which is what the pick was made on. */
  scenario_over_probability?: number
  scenario_under_probability?: number
  scenario_probability?: number
  scenario_conditioning?: string
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
  result: null | {
    away_score: number
    home_score: number
    innings?: null | { away: (number | null)[]; home: (number | null)[] }
  }
  prediction: Prediction | null
  replay_prediction: Prediction | null
  market: null | {
    provider: string
    bookmaker_count: number
    total_line: number | null
    home_spread: number | null
    /** The book's de-vigged probability that the home club covers `home_spread`. */
    home_spread_probability?: number | null
    /** The book's de-vigged probability that the total goes over `total_line`. */
    total_over_probability?: number | null
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
    batting_side: string | null
    platoon_opponent_hand: string | null
    platoon_plate_appearances: number | null
    platoon_ops: number | null
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
