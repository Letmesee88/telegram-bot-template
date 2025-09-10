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
