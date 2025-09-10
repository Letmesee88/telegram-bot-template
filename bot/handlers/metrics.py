import prometheus_client
from aiohttp import web
from aiohttp.web_request import Request
from aiohttp.web_response import Response

"""
Prometheus metrics registry
- FoodAI counters: started/succeeded/failed
- FoodAI duration histogram (milliseconds)
"""

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


class MetricsView(web.View):
    def __init__(
        self,
        request: Request,
        registry: prometheus_client.CollectorRegistry = prometheus_client.REGISTRY,
    ) -> None:
        self._request = request
        self.registry = registry

    async def get(self) -> Response:
        response = Response(body=prometheus_client.generate_latest(self.registry))
        response.content_type = prometheus_client.CONTENT_TYPE_LATEST
        return response
