from typing import Any

from aiohttp import web
from aiohttp.web_request import Request
from aiohttp.web_response import Response

# Re-export metrics from a separate module to avoid circular imports and duplicate registries
from bot.metrics import (  # noqa: F401
    foodai_analysis_text_rewrite,
    foodai_duration_ms,
    foodai_edit_applied,
    foodai_edit_duration_ms,
    foodai_edit_failed,
    foodai_edit_started,
    foodai_failed,
    foodai_file_url_missing,
    foodai_itogo_shown,
    foodai_lexicon_is_food,
    foodai_not_food,
    foodai_precheck_error,
    foodai_precheck_is_food,
    foodai_precheck_not_food,
    foodai_provider_error,
    foodai_started,
    foodai_succeeded,
    prometheus_client,
)


class MetricsView(web.View):
    def __init__(
        self,
        request: Request,
        registry: Any = prometheus_client.REGISTRY,
    ) -> None:
        self._request = request
        self.registry = registry

    async def get(self) -> Response:
        response = Response(body=prometheus_client.generate_latest(self.registry))
        response.content_type = prometheus_client.CONTENT_TYPE_LATEST
        return response
