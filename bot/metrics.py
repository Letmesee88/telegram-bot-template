try:
    import prometheus_client  # type: ignore
except Exception:  # pragma: no cover - test env fallback
    class _NoopLabels:
        def inc(self, *args, **kwargs):
            return None

        def observe(self, *args, **kwargs):
            return None

    class _NoopCounter:
        def __init__(self, *args, **kwargs):
            pass

        def labels(self, *args, **kwargs):
            return _NoopLabels()

    class _NoopHistogram(_NoopCounter):
        pass

    class _NoopRegistry:
        pass

    class _NoopProm:
        Counter = _NoopCounter
        Histogram = _NoopHistogram
        REGISTRY = _NoopRegistry()
        CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"

        @staticmethod
        def generate_latest(*args, **kwargs):
            return b""

    prometheus_client = _NoopProm()  # type: ignore

# Note: keep label sets LOW cardinality.
foodai_started = prometheus_client.Counter(
    "foodai_started_total",
    "FoodAI analyze started",
    ["source"],  # photo|text
)
foodai_succeeded = prometheus_client.Counter(
    "foodai_succeeded_total",
    "FoodAI analyze succeeded",
    ["source"],
)
foodai_failed = prometheus_client.Counter(
    "foodai_failed_total",
    "FoodAI analyze failed",
    ["source"],
)
foodai_duration_ms = prometheus_client.Histogram(
    "foodai_duration_ms",
    "FoodAI analyze duration in milliseconds",
    buckets=[50, 100, 200, 400, 800, 1600, 3200, 6400],
)

# Preview contained per-meal percent block ("Итого % от нормы").
foodai_itogo_shown = prometheus_client.Counter(
    "foodai_preview_itogo_shown_total",
    "FoodAI preview included per-meal percent block",
    ["source"],  # photo|text
)

# Not-food cases flagged by model
foodai_not_food = prometheus_client.Counter(
    "foodai_not_food_total",
    "FoodAI input flagged as not containing food/drink",
    ["source"],  # photo|text
)

# Pre-check (foodness) outcomes
foodai_precheck_is_food = prometheus_client.Counter(
    "foodai_precheck_is_food_total",
    "FoodAI precheck decided input contains food/drink",
    ["source"],  # photo|text
)
foodai_precheck_not_food = prometheus_client.Counter(
    "foodai_precheck_not_food_total",
    "FoodAI precheck decided input does not contain food/drink",
    ["source"],  # photo|text
)
foodai_precheck_error = prometheus_client.Counter(
    "foodai_precheck_error_total",
    "FoodAI precheck failed",
    ["source", "reason"],  # photo|text | timeout|json|http|other
)

# Lexicon-based positive match (text only)
foodai_lexicon_is_food = prometheus_client.Counter(
    "foodai_lexicon_is_food_total",
    "FoodAI text matched simple food/beverage lexicon (precheck bypass)",
    ["source"],  # text
)

# Provider and transport errors
foodai_provider_error = prometheus_client.Counter(
    "foodai_provider_error_total",
    "Errors during provider processing or parsing",
    ["source", "error"],  # photo|text | provider_unavailable|parse_error
)
foodai_file_url_missing = prometheus_client.Counter(
    "foodai_file_url_missing_total",
    "Telegram file_url could not be resolved",
    ["source"],  # photo
)

# analysis_text was rewritten by post-processor (hybrid pipeline)
foodai_analysis_text_rewrite = prometheus_client.Counter(
    "foodai_analysis_text_rewrite_total",
    "FoodAI analysis_text was rewritten by post-processor",
    ["reason"],  # length|cliche|components|citation|empty|timeout|error
)

# ===== Edit flow metrics =====
# Keep label sets LOW cardinality: action ∈ {add,remove,replace,scale,change_qty,unknown}
foodai_edit_started = prometheus_client.Counter(
    "foodai_edit_started_total",
    "FoodAI edit flow started (instruction parsed)",
    ["action"],
)
foodai_edit_applied = prometheus_client.Counter(
    "foodai_edit_applied_total",
    "FoodAI edit flow applied successfully",
    ["action"],
)
foodai_edit_failed = prometheus_client.Counter(
    "foodai_edit_failed_total",
    "FoodAI edit flow failed",
    ["action", "reason"],  # reason ∈ {parse,ambiguous,not_found,caps,unsupported,other}
)
foodai_edit_duration_ms = prometheus_client.Histogram(
    "foodai_edit_duration_ms",
    "FoodAI edit flow duration in milliseconds",
    buckets=[50, 100, 200, 400, 800, 1600, 3200, 6400],
)

# ===== Edit NLU metrics =====
# Keep labels low-cardinality
foodai_edit_nlu_started = prometheus_client.Counter(
    "foodai_edit_nlu_started_total",
    "FoodAI edit NLU started",
)
foodai_edit_nlu_succeeded = prometheus_client.Counter(
    "foodai_edit_nlu_succeeded_total",
    "FoodAI edit NLU succeeded",
)
foodai_edit_nlu_failed = prometheus_client.Counter(
    "foodai_edit_nlu_failed_total",
    "FoodAI edit NLU failed",
    ["reason"],  # provider_unavailable|parse_json|bad_action|... as coded in adapter
)
foodai_edit_nlu_duration_ms = prometheus_client.Histogram(
    "foodai_edit_nlu_duration_ms",
    "FoodAI edit NLU duration in milliseconds",
    buckets=[50, 100, 200, 400, 800, 1600, 3200, 6400],
)

# ===== Vision escalation metrics =====
# Keep label sets LOW cardinality. Models are finite and reasons are a small fixed set.
foodai_escalation_attempts = prometheus_client.Counter(
    "foodai_escalation_attempts_total",
    "FoodAI vision escalation attempts (per step)",
    ["from_model", "to_model", "reason"],  # reason: attempt
)
foodai_escalation_success = prometheus_client.Counter(
    "foodai_escalation_success_total",
    "FoodAI vision escalation chain succeeded (final stop)",
    ["from_model", "to_model", "reason"],  # reason: final
)
foodai_escalation_failed = prometheus_client.Counter(
    "foodai_escalation_failed_total",
    "FoodAI vision escalation chain failed or exhausted",
    ["from_model", "to_model", "reason"],  # reason: steps_exhausted|weak|provider_error|timeout
)
foodai_escalation_attempt_dur_ms = prometheus_client.Histogram(
    "foodai_escalation_attempt_dur_ms",
    "FoodAI vision escalation attempt duration in milliseconds",
    ["model", "detail"],
    buckets=[50, 100, 200, 400, 800, 1600, 3200, 6400],
)
foodai_escalation_total_dur_ms = prometheus_client.Histogram(
    "foodai_escalation_total_dur_ms",
    "FoodAI vision escalation total chain duration in milliseconds",
    buckets=[50, 100, 200, 400, 800, 1600, 3200, 6400],
)

# ===== Recommender metrics =====
# Keep label sets LOW cardinality. reason ∈ {timeout,json_parse,invalid,provider_error,empty,other}
recommender_started = prometheus_client.Counter(
    "recommender_started_total",
    "Recommender started",
    ["meal_type"],
)
recommender_succeeded = prometheus_client.Counter(
    "recommender_succeeded_total",
    "Recommender succeeded",
    ["meal_type"],
)
recommender_failed = prometheus_client.Counter(
    "recommender_failed_total",
    "Recommender failed",
    ["meal_type", "reason"],
)
recommender_duration_ms = prometheus_client.Histogram(
    "recommender_duration_ms",
    "Recommender duration in milliseconds",
    ["meal_type"],
    buckets=[50, 100, 200, 400, 800, 1600, 3200, 6400, 12800],
)
