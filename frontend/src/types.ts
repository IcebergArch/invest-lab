export interface Returns {
  '1d'?: number | null;
  '5d'?: number | null;
  '20d'?: number | null;
}

export interface BroadIndex {
  name?: string;
  instrument_id?: string;
  returns?: Returns;
}

export interface IndustryGroup {
  name?: string;
  returns?: Returns;
  share_change_pp?: number | null;
  up_1d?: number;
  member_count?: number;
}

export interface MarketReport {
  status?: string;
  reason?: string;
  asof?: string;
  industry_asof?: string;
  broad?: BroadIndex[];
  groups?: IndustryGroup[];
  industry_breadth?: { up?: number; down?: number; total?: number };
  activity_available?: boolean;
  net_flow_available?: boolean;
}

export interface FlowReport {
  reader_lines?: string[];
  date_line?: string;
  net_flow_line?: string;
}

export interface ScreenReport {
  status?: string;
  reason?: string;
  asof?: string;
  expected_universe_count?: number | null;
  universe_count?: number;
  data_ready_count?: number;
}

export interface ForecastSummary {
  status?: string;
  reason?: string;
  universe_scope?: string;
  pooled?: Record<string, {
    status?: string;
    mae_return_skill_vs_random_walk?: number | null;
  }>;
}

export interface BacktestMetrics {
  total_return?: number | null;
  cagr?: number | null;
  max_drawdown?: number | null;
  sharpe_rf0?: number | null;
}

export interface ResearchSummary {
  universe?: string[];
  backtest?: {
    metrics?: BacktestMetrics;
    prospective_out_of_sample?: { status?: string };
  };
}

export interface RecommendedStock {
  instrument_id: string;
  name?: string;
  industry_group?: string;
  asof?: string;
  basis?: string;
  risk_metrics?: {
    momentum_20d?: number | null;
    momentum_60d?: number | null;
    max_drawdown_60d?: number | null;
    annualized_volatility_20d?: number | null;
  };
  forecast_expected_return_20d?: number | null;
  forecast_p10_return_20d?: number | null;
}

export interface RecommendationsReport {
  status?: string;
  reason?: string;
  asof?: string;
  recommendations?: RecommendedStock[];
  checks?: { key?: string; passed?: boolean; detail?: string }[];
}

export interface HistoricalArchiveSummary {
  status?: string;
  source_id?: string;
  snapshot_id?: string;
  catalogue_scope?: string;
  catalogue_collected_at?: string;
  catalogue_stock_count?: number;
  stock_count?: number;
  completed_window_count?: number;
  bar_count?: number;
  failed_window_count?: number;
  adjustment?: string;
  eligible_for_screening?: boolean;
  published_at?: string;
}

export interface QlibArchiveSummary {
  status: 'inspected';
  source_id: string;
  release_tag: string;
  calendar_first: string;
  calendar_last: string;
  stock_count_with_valid_close: number;
  bar_count: number;
  inactive_reference_covered: number;
  adjustment: 'qlib_adjusted';
  eligible_for_screening: false;
  validated_at: string;
}

export interface DashboardPayload {
  market: MarketReport;
  flow: FlowReport;
  stock_shortlist: ScreenReport;
  recommendations: RecommendationsReport;
  historical_archive?: HistoricalArchiveSummary;
  qlib_archive?: QlibArchiveSummary;
  research: ResearchSummary;
  forecast: ForecastSummary;
  run?: { stock_signal_asof?: string };
}

export interface StrategyMetrics extends BacktestMetrics {
  annual_volatility?: number | null;
  turnover?: number | null;
}

export interface HistoricalBackfillBatchSummary {
  status?: 'ready' | 'missing_batch' | 'unavailable';
  file_modified_at?: string;
  last_batch?: {
    run_id?: string;
    source_id?: string;
    snapshot_id?: string;
    requested_window_count?: number;
    completed_window_count?: number;
    empty_window_count?: number;
    failed_window_count?: number;
    remaining_window_count?: number;
    deferred_failure_count?: number;
    bar_count?: number;
    status_count?: number;
    elapsed_seconds?: number;
    error?: string | null;
  } | null;
}

export interface NorthStarScore {
  policy_version?: string;
  status?: string;
  score?: number;
  benchmark?: string;
  components?: {
    return?: { weight?: number; normalized_score?: number; difference?: number };
    drawdown?: { weight?: number; normalized_score?: number; difference?: number };
  };
}

export interface ConsoleStrategy {
  strategy_id?: string;
  strategy_version?: string;
  feature_version?: string;
  factor_ids?: string[];
  description?: string;
  implementation_status?: string;
  north_star?: NorthStarScore;
  status?: string;
  origin?: string | null;
  record_id?: string | null;
  research_sha256?: string | null;
  source_run_id?: string;
  asof?: string;
  parameters?: Record<string, unknown>;
  universe_scope?: string;
  universe_count?: number;
  backtest?: {
    status?: string;
    reason?: string;
    start?: string;
    end?: string;
    observations?: number;
    metrics?: StrategyMetrics;
    benchmarks?: {
      focus_equal_weight_hold?: { status?: string; metrics?: StrategyMetrics };
      csi300_price_reference?: { status?: string; reference_only?: boolean; metrics?: StrategyMetrics };
    };
    retrospective_split?: {
      status?: string;
      independent_out_of_sample?: boolean;
      reason?: string;
      early_period?: { start?: string; end?: string; sessions?: number; strategy_metrics?: StrategyMetrics; equal_hold_metrics?: StrategyMetrics };
      recent_period?: { start?: string; end?: string; sessions?: number; strategy_metrics?: StrategyMetrics; equal_hold_metrics?: StrategyMetrics };
    };
    prospective_out_of_sample?: { status?: string; matured_sessions?: number; reason?: string };
    execution_assumption?: string;
    cost_rate?: number;
    universe?: string[];
    input_fingerprint_sha256?: string;
  };
  limitations?: string[];
}

export interface AlgorithmComponent {
  algorithm_id?: string;
  version?: string;
  display_name?: string;
  provider?: string;
  role?: string;
  strategy_roles?: string[];
  implementation_status?: string;
  research_status?: string;
  status?: string;
  model_id?: string | null;
  model_revision?: string | null;
  adapter?: string | null;
  strategy_ids?: string[];
  evidence?: {
    kind?: 'forecast_experiment' | 'stability_audit';
    record_id?: string;
    dataset?: string;
    created_at?: string;
    source_record_id?: string;
    provider_status?: 'ready' | 'partial_failure' | string;
    horizons?: Record<string, { sample_count?: number }>;
  }[];
  next_gate?: string;
  source_url?: string | null;
}

export interface ConsoleStatus {
  status?: string;
  generated_at?: string;
  china_today?: string;
  tasks?: {
    status?: string;
    reason?: string;
    active?: { task_id?: string; kind?: string; status?: string; created_at?: string } | null;
    recent?: {
      task_id?: string;
      kind?: string;
      status?: string;
      created_at?: string;
      finished_at?: string;
      result?: {
        status?: string;
        reason?: string;
        runs?: { strategy_id?: string; status?: string; optimization_id?: string; score?: number; reason?: string }[];
        saved?: number;
        pending?: number;
        unavailable?: number;
        market_asof?: string;
      };
      reason?: string;
    }[];
  };
  published_run?: {
    run_id?: string;
    generated_at?: string;
    market_broad_asof?: string;
    market_industry_asof?: string;
    stock_signal_asof?: string;
    stock_screen_status?: string;
    recommendation_status?: string;
    recommendation_count?: number;
    strategy_id?: string;
    strategy_version?: string;
  };
  market_store?: {
    status?: string;
    groups?: Record<string, { instrument_count?: number; bar_count?: number; first_date?: string | null; last_date?: string | null }>;
    latest_sync_runs?: {
      run_id?: string;
      status?: string;
      started_at?: string;
      finished_at?: string | null;
      requested_start?: string;
      requested_end?: string;
      instrument_count?: number;
      row_count?: number;
      error?: string | null;
    }[];
  };
  historical_archive?: HistoricalArchiveSummary;
  historical_backfill_batch?: HistoricalBackfillBatchSummary;
  qlib_archive?: QlibArchiveSummary;
  research_journal?: {
    status?: string;
    recent?: {
      record_id?: string;
      generated_at?: string;
      instrument_id?: string;
      name?: string;
      asof?: string;
      status?: string;
      payload_sha256?: string;
    }[];
  };
  strategies?: ConsoleStrategy[];
  algorithm_components?: AlgorithmComponent[];
  strategy_archive?: { status?: string; reason?: string; record_count?: number };
  experiments?: {
    status?: string;
    reason?: string;
    recent?: {
      experiment_id?: string;
      snapshot_record_id?: string;
      recorded_at?: string;
      origin?: string;
      source_run_id?: string | null;
      strategy_id?: string;
      strategy_version?: string;
      asof?: string;
      state?: string;
      input_fingerprint_sha256?: string;
      parameters?: Record<string, unknown>;
      cost_rate?: number;
      execution_assumption?: string;
      universe_count?: number;
      metrics?: StrategyMetrics;
      north_star?: NorthStarScore;
      validation?: { prospective_status?: string; matured_sessions?: number };
    }[];
  };
  forecast_experiments?: {
    status?: string;
    datasets?: {
      dataset?: 'focus' | 'qlib';
      status?: string;
      reason?: string;
      latest?: {
        record_id?: string;
        created_at?: string;
        benchmark_version?: string;
        experiment_key_sha256?: string;
        input_fingerprint_sha256?: string;
        price_basis?: string;
        data_start?: string;
        data_asof?: string;
        stock_count?: number;
        common_sessions?: number | null;
        horizon_unit?: string;
        configuration?: { min_context?: number; step?: number };
        research_only?: boolean;
        point_in_time_validated?: boolean;
        prospective_out_of_sample?: boolean;
        provider_count?: number;
        providers?: {
          name?: string;
          model_id?: string;
          model_revision?: string | null;
          status?: string;
          reason?: string | null;
          pretraining_overlap_status?: string;
          stocks_with_samples?: number | null;
          horizons?: Record<string, {
            status?: string;
            sample_count?: number | null;
            coverage?: number | null;
            mae_return_skill_vs_random_walk?: number | null;
          }>;
        }[];
      } | null;
    }[];
  };
  forecast_stability?: {
    status?: 'ready' | 'partial' | 'empty' | 'unavailable';
    reason?: string;
    latest?: {
      record_id?: string;
      created_at?: string;
      model_name?: string;
      model_revision?: string;
      source_benchmark_record_id?: string;
      event_study_record_id?: string;
      event_mask_use?: 'retrospective_diagnostic_only';
      point_in_time_validated?: boolean;
      prospective_out_of_sample?: boolean;
      horizons?: Record<string, {
        tier?: 'broadly_stable' | 'conditional_scope_only' | 'coarse_or_unstable';
        recommendation?: string;
        sample_count?: number;
        core_count?: number;
        event_stress_count?: number;
        candidate_coverage?: number | null;
        mae_return_skill_vs_random_walk?: number | null;
        block_bootstrap_95pct?: [number | null, number | null];
      }>;
    }[];
  };
  group_behavior_pilot?: {
    status?: 'ready' | 'partial' | 'empty' | 'unavailable';
    reason?: string;
    latest?: {
      record_id?: string;
      created_at?: string;
      model_name?: string;
      cohort_size?: number;
      calendar_end?: string;
      reviewed_event_count?: number;
      point_in_time_validated?: boolean;
      prospective_out_of_sample?: boolean;
      event_origin_policy?: string;
      horizons?: Record<string, {
        regular_paired_count?: number;
        regular_candidate_origins?: number;
        regular_coverage?: number | null;
        regular_mae_skill_vs_zero_return?: number | null;
        policy_stress_paired_count?: number;
        policy_stress_mae_skill_vs_zero_return?: number | null;
      }>;
    } | null;
  };
  raw_data_inventory?: {
    status?: 'ready' | 'partial' | 'unavailable';
    reason?: string;
    inventory_version?: string;
    snapshot_at?: string;
    snapshot_age_minutes?: number;
    report_filename?: string;
    main?: {
      status?: string;
      reason?: string;
      stock_count?: number;
      bar_count?: number;
      volume_unit?: string;
      amount_cny_rows?: number;
      amount_null_rows?: number;
      latest_run?: { run_id?: string; status_at_snapshot?: string };
    };
    historical?: {
      status?: string;
      reason?: string;
      stock_count?: number;
      bar_count?: number;
      volume_unit?: string;
      amount_cny_rows?: number;
      amount_null_rows?: number;
      raw_amount_cny_rows?: number;
      raw_amount_only_rows?: number;
      status_rows?: number;
      catalogue_stock_count?: number;
      archive_complete?: boolean;
      latest_run?: { run_id?: string; status_at_snapshot?: string };
      zero_stock_years?: string[];
      year_exchange?: { year: number; sh_bars: number; sz_bars: number }[];
    };
    qlib?: {
      status?: string;
      reason?: string;
      stock_count?: number;
      calendar_start?: string;
      calendar_end?: string;
      calendar_sessions?: number;
      feature_fields?: string[];
      feature_count?: number;
      files_per_feature?: number;
      field_coverage_is_file_presence_only?: boolean;
      volume_unit?: string;
      amount_unit?: string;
    };
  };
  optimizations?: {
    status?: string;
    reason?: string;
    recent?: {
      optimization_id?: string;
      created_at?: string;
      payload_sha256?: string;
      optimizer_version?: string;
      strategy_id?: string;
      strategy_version?: string;
      objective_policy_version?: string;
      canonical_contract_version?: string;
      input_fingerprint_sha256?: string;
      universe_count?: number;
      candidate_count?: number;
      selected_candidate_index?: number;
      selected_parameters?: Record<string, unknown>;
      selection_rule?: string;
      train?: { start?: string; end?: string; sessions?: number; selected_metrics?: StrategyMetrics; selected_north_star?: NorthStarScore; benchmark_metrics?: StrategyMetrics };
      historical_holdout?: { status?: string; start?: string; end?: string; sessions?: number; metrics?: StrategyMetrics; benchmark_metrics?: StrategyMetrics; north_star?: NorthStarScore; reason?: string };
      promotion_status?: string;
      promotion_reason?: string;
    }[];
  };
  feedback_cases?: {
    status?: string;
    reason?: string;
    recent?: {
      case_id?: string;
      created_at?: string;
      research_record_id?: string;
      instrument_id?: string;
      name?: string | null;
      report_asof?: string;
      decision?: string;
      observed_date?: string;
      review_status?: string;
      latest_review_id?: string;
      latest_review_horizon_sessions?: number;
    }[];
  };
  reviews?: {
    status?: string;
    reason?: string;
    recent?: {
      review_id?: string;
      case_id?: string;
      created_at?: string;
      status?: string;
      horizon_sessions?: number | null;
      outcome?: Record<string, unknown> | null;
      market_asof?: string | null;
      reason?: string | null;
    }[];
  };
  factors?: {
    factor_id?: string;
    version?: string;
    description?: string;
    input_field?: string;
    input_requirement?: string;
    parameter_name?: string;
    minimum_history?: string;
    output_unit?: string;
  }[];
  rules?: { rule_id?: string; version?: string; backtest_status?: string; reason?: string }[];
  versions?: Record<string, string | null>;
}

export interface FeedbackCaseInput {
  research_record_id: string;
  research_record_date: string;
  decision: 'watch' | 'skip' | 'buy' | 'sell';
  observed_date: string;
  executed_price?: number;
  quantity?: number;
  note?: string;
}

export interface FeedbackCaseResult {
  status: string;
  case_id?: string;
  created_at?: string;
}

export interface AcceptedTask {
  status: 'accepted';
  task_id: string;
  kind: 'optimize' | 'review_cases';
  created_at?: string;
}

export interface StockMetrics {
  return_5d?: number | null;
  return_20d?: number | null;
  return_60d?: number | null;
  annualized_volatility_20d?: number | null;
  max_drawdown_60d?: number | null;
  avg_amount_20d_cny?: number | null;
}

export interface StockDecisionSide {
  stance?: 'consider' | 'wait' | 'avoid' | 'hold' | 'consider_reduce' | 'review_exit' | 'unavailable';
  label?: string;
  reason?: string;
}

export interface StockDecision {
  status?: 'ready' | 'insufficient_evidence';
  rule_version?: string;
  asof?: string | null;
  horizon?: string | null;
  reason?: string | null;
  buy?: StockDecisionSide | null;
  sell?: StockDecisionSide | null;
  reasons?: string[];
  risks?: string[];
  cost_context?: string | null;
  next_check?: string | null;
  input_provenance?: Record<string, unknown>;
  signals?: { key: string; value: number; asof: string }[];
}

export interface StockChart {
  status?: 'ready' | 'insufficient_data';
  chart_version?: string;
  asof?: string;
  price_basis?: 'qfq_cny' | 'qlib_adjusted';
  history?: { date: string; close: number; gap_before?: boolean }[];
  signals?: { date: string; kind: 'buy' | 'sell'; rule_id: string; rule_version: string }[];
  rule_position?: { date: string; position: 0 | 1 }[];
  rule_id?: string;
  rule_version?: string;
  signal_rule?: {
    rule_id?: string;
    rule_version?: string;
    status?: string;
    execution_assumption?: string;
  };
  forecast?: {
    status?: 'validated' | 'research_only' | 'unavailable';
    price_basis?: 'qfq_cny' | 'qlib_adjusted';
    model_id?: string;
    model_version?: string;
    asof?: string;
    validation?: {
      status?: 'passed' | 'failed' | 'research_only' | 'exploratory' | 'insufficient_data';
      method?: string;
      sample_count?: number;
      metric_label?: string;
      metric_value?: number;
      skill_vs_random_walk?: number | null;
    };
    points?: { step: number; median: number; p10?: number | null; p90?: number | null }[];
  };
}

export interface StockAnalysis {
  status: string;
  reason?: string | null;
  instrument_id?: string | null;
  name?: string | null;
  asof?: string | null;
  latest_close?: number | null;
  adjusted_close?: number | null;
  cost_price?: number | null;
  cost_return?: number | null;
  summary?: string;
  decision?: StockDecision | null;
  chart?: StockChart | null;
  research_record?: {
    status?: 'saved' | 'save_failed';
    record_id?: string;
    generated_at?: string;
    payload_sha256?: string;
    reason?: string;
  } | null;
  trend?: { label?: string; detail?: string; metrics?: StockMetrics } | null;
  risk?: { summary?: string; flags?: string[]; metrics?: StockMetrics } | null;
  forecast?: {
    status?: string;
    method?: string;
    baseline_close_5d?: number | null;
    baseline_close_20d?: number | null;
    rolling_validation?: Record<string, {
      status?: string;
      count?: number;
      mean_absolute_return_error?: number | null;
    }>;
    explanation?: string;
    model_research_status?: string;
  } | null;
  backtest?: {
    status?: string;
    reason?: string;
    start?: string;
    end?: string;
    observations?: number;
    strategy_metrics?: BacktestMetrics;
    buy_hold_metrics?: BacktestMetrics;
  } | null;
  sources?: {
    latest_source_id?: string;
    last_trade_date?: string;
    bar_count?: number | null;
    latest_run_id?: string | null;
    release_tag?: string;
    release_target_trade_date?: string;
    archive_sha256?: string;
    adjustment?: string;
    analyzed_contiguous_close_rows?: number;
    read_window_calendar_rows?: number;
    first_valid_close_date?: string | null;
    last_valid_close_date?: string | null;
    full_history_missing_close_count?: number | null;
    factor_verified?: boolean;
    amount_verified?: boolean;
  }[];
  limitations?: string[];
}
