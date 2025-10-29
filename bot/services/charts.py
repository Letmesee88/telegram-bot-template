from __future__ import annotations
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional, Tuple, List
try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
    _PIL_OK = True
except Exception:
    Image = ImageDraw = ImageFont = None  # type: ignore
    _PIL_OK = False

import aiohttp
import asyncio
import urllib.parse
from loguru import logger

from bot.core.config import settings
from bot.cache.redis import cached, build_key


@dataclass
class Projection:
    labels: List[str]
    values: List[float]
    band_low: Optional[List[float]] = None
    band_high: Optional[List[float]] = None
    target_value: Optional[float] = None
    mode: str = "percent"  # percent|kg


def _privacy_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _date_range_by_week(start: date, end: date) -> List[date]:
    days = []
    cur = start
    if end < start:
        end = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=7)
    if not days or days[-1] != end:
        days.append(end)
    return days


def _ease_progress(idx: int, total_steps: int) -> float:
    if total_steps <= 0:
        return 1.0
    t = idx / float(total_steps)
    if getattr(settings, "CHARTS_EASE", "linear") == "ease_out":
        power = max(1.0, float(getattr(settings, "CHARTS_EASE_POWER", 2.0) or 2.0))
        return 1.0 - pow(1.0 - t, power)
    # linear
    return t


def _build_projection_percent(start_w: float, goal_w: Optional[float], weekly_rate: float,
                              start_date: date, eta_date: Optional[date]) -> Projection:
    horizon_end = eta_date or (start_date + timedelta(weeks=12))
    dates = _date_range_by_week(start_date, horizon_end)

    def to_pct(w: float) -> float:
        return round(w / start_w * 100.0, 2)

    values: List[float] = []
    steps = max(1, len(dates) - 1)
    for i, _ in enumerate(dates):
        if goal_w is not None and weekly_rate > 0:
            # Smooth curve from start to goal by easing function
            prog = _ease_progress(i, steps)
            cur_w = start_w + (goal_w - start_w) * prog
        elif weekly_rate > 0 and goal_w is None:
            cur_w = start_w + weekly_rate * i
        else:
            cur_w = start_w
        values.append(to_pct(cur_w))

    target_value = to_pct(goal_w) if goal_w is not None else None

    band_frac = settings.CHARTS_BAND_FRAC
    band_low: Optional[List[float]] = None
    band_high: Optional[List[float]] = None
    if weekly_rate and band_frac > 0:
        low: List[float] = []
        high: List[float] = []
        for i, v in enumerate(values):
            # corridor around the curve based on ±band around weekly rate (linear approx)
            delta_kg = weekly_rate * i
            delta_low = delta_kg * (1 - band_frac)
            delta_high = delta_kg * (1 + band_frac)
            if goal_w is not None:
                if goal_w < start_w:  # lose
                    w_low = max(start_w - delta_high, goal_w)
                    w_high = max(start_w - delta_low, goal_w)
                else:  # gain
                    w_low = min(start_w + delta_low, goal_w)
                    w_high = min(start_w + delta_high, goal_w)
            else:
                # no goal: free trajectory
                if goal_w is None and weekly_rate >= 0:
                    w_low = start_w + delta_low
                    w_high = start_w + delta_high
                else:
                    w_low = start_w - delta_high
                    w_high = start_w - delta_low
            low.append(round(w_low / start_w * 100.0, 2))
            high.append(round(w_high / start_w * 100.0, 2))
        band_low = low
        band_high = high

    labels = [d.strftime("%d.%m") for d in dates]
    return Projection(labels=labels, values=values, band_low=band_low, band_high=band_high,
                      target_value=target_value, mode="percent")


def _build_projection_kg(start_w: float, goal_w: Optional[float], weekly_rate: float,
                         start_date: date, eta_date: Optional[date]) -> Projection:
    horizon_end = eta_date or (start_date + timedelta(weeks=12))
    dates = _date_range_by_week(start_date, horizon_end)

    values: List[float] = []
    steps = max(1, len(dates) - 1)
    for i, _ in enumerate(dates):
        if goal_w is not None and weekly_rate > 0:
            prog = _ease_progress(i, steps)
            cur_w = start_w + (goal_w - start_w) * prog
        elif weekly_rate > 0 and goal_w is None:
            cur_w = start_w + weekly_rate * i
        else:
            cur_w = start_w
        values.append(round(cur_w, 2))

    target_value = round(goal_w, 2) if goal_w is not None else None

    band_frac = settings.CHARTS_BAND_FRAC
    band_low: Optional[List[float]] = None
    band_high: Optional[List[float]] = None
    if weekly_rate and band_frac > 0:
        low: List[float] = []
        high: List[float] = []
        for i, _ in enumerate(values):
            delta_kg = weekly_rate * i
            delta_low = delta_kg * (1 - band_frac)
            delta_high = delta_kg * (1 + band_frac)
            if goal_w is not None:
                if goal_w < start_w:
                    w_low = max(start_w - delta_high, goal_w)
                    w_high = max(start_w - delta_low, goal_w)
                else:
                    w_low = min(start_w + delta_low, goal_w)
                    w_high = min(start_w + delta_high, goal_w)
            else:
                if weekly_rate >= 0:
                    w_low = start_w + delta_low
                    w_high = start_w + delta_high
                else:
                    w_low = start_w - delta_high
                    w_high = start_w - delta_low
            low.append(round(w_low, 2))
            high.append(round(w_high, 2))
        band_low = low
        band_high = high

    labels = [d.strftime("%d.%m") for d in dates]
    return Projection(labels=labels, values=values, band_low=band_low, band_high=band_high,
                      target_value=target_value, mode="kg")


def build_projection(start_weight: float, goal_weight: Optional[float], weekly_rate: float,
                     start_date: date, eta_date: Optional[date], mode: str = "percent") -> Projection:
    rate = abs(weekly_rate)
    if mode == "kg":
        return _build_projection_kg(start_weight, goal_weight, rate, start_date, eta_date)
    return _build_projection_percent(start_weight, goal_weight, rate, start_date, eta_date)


def _chart_config_from_projection(p: Projection) -> dict:
    # Theme colors (overridable via settings)
    bg = getattr(settings, "CHARTS_COLOR_BG", None) or "#0b1220"
    grid = getattr(settings, "CHARTS_COLOR_GRID", None) or "#203049"
    axis = getattr(settings, "CHARTS_COLOR_AXIS", None) or "#94a3b8"
    line = getattr(settings, "CHARTS_COLOR_LINE", None) or "#22d3ee"
    band = getattr(settings, "CHARTS_COLOR_BAND", None) or "#22d3ee33"
    target = "#ffffff88"
    label_bg = getattr(settings, "CHARTS_COLOR_LABEL_BG", None) or "#ffffff"
    label_fg = getattr(settings, "CHARTS_COLOR_LABEL_FG", None) or "#1e293b"

    main_bg = "#22d3ee26" if getattr(settings, "CHARTS_FILL_MAIN", True) else line
    datasets = [
        {
            "label": "Прогноз",
            "data": p.values,
            "borderColor": line,
            "backgroundColor": main_bg,
            "tension": 0.35,
            "pointRadius": 0,
            "borderWidth": 4,
            "fill": getattr(settings, "CHARTS_FILL_MAIN", True),
            "datalabels": {"display": False},
        }
    ]
    # Dedicated dataset for endpoint labels only
    if getattr(settings, "CHARTS_USE_DATALABELS", True) and p.values:
        labels_ds = [None for _ in p.labels]
        labels_ds[0] = p.values[0]
        labels_ds[-1] = p.values[-1]
        datasets.append({
            "label": "Подписи",
            "data": labels_ds,
            "borderColor": "rgba(0,0,0,0)",
            "backgroundColor": "rgba(0,0,0,0)",
            "pointRadius": 0,
            "showLine": False,
            "order": -2,
            "datalabels": {
                "display": True,
                "align": "function(ctx){var i=ctx.dataIndex;var n=ctx.dataset.data.length-1;return i===0?'left':(i===n?'right':'center');}",
                "backgroundColor": label_bg,
                "color": label_fg,
                "borderRadius": 4,
                "padding": {"left": 8, "right": 8, "top": 3, "bottom": 3},
                "font": {"weight": "700", "size": 16},
                "formatter": "function(v,ctx){var yTitle=ctx.chart.config.options.scales.y.title.text||'';var s=''+v; if(yTitle.indexOf('%')>=0){s+='%';} return s;}"
            }
        })
    if p.band_low and p.band_high:
        datasets.append({
            "label": "Нижн. коридор",
            "data": p.band_low,
            "borderColor": band,
            "backgroundColor": band,
            "fill": "+1",
            "pointRadius": 0,
            "tension": 0.35,
            "borderWidth": 0,
            "order": 1,
            "datalabels": {"display": False},
        })
        datasets.append({
            "label": "Верхн. коридор",
            "data": p.band_high,
            "borderColor": band,
            "backgroundColor": band,
            "fill": False,
            "pointRadius": 0,
            "tension": 0.35,
            "borderWidth": 0,
            "order": 0,
            "datalabels": {"display": False},
        })
    # Optional start/goal markers
    if getattr(settings, "CHARTS_SHOW_MARKERS", True):
        start_marker = [None for _ in p.labels]
        goal_marker = [None for _ in p.labels]
        if p.values:
            start_marker[0] = p.values[0]
            goal_marker[-1] = p.values[-1]
        datasets.append({
            "label": "Старт",
            "data": start_marker,
            "borderColor": line,
            "backgroundColor": line,
            "pointRadius": 7,
            "showLine": False,
            "order": -1,
            "datalabels": {"display": False},
        })
        datasets.append({
            "label": "Цель",
            "data": goal_marker,
            "borderColor": "#ffffff",
            "backgroundColor": "#ffffff",
            "pointRadius": 7,
            "showLine": False,
            "order": -1,
            "datalabels": {"display": False},
        })

    # Compute symmetric Y-range around the target so the dashed line is centered
    y_min = None
    y_max = None
    if p.mode == "kg" and p.target_value is not None and p.values:
        start_y = p.values[0]
        target_y = p.target_value
        # Center around midpoint between start and goal, add breathing margin
        margin = float(getattr(settings, "CHARTS_Y_MARGIN_KG", 1.0))
        center = (start_y + target_y) / 2.0
        half_range = abs(start_y - target_y) / 2.0
        extent = half_range + margin
        y_min = math.floor(center - extent)
        y_max = math.ceil(center + extent)

    # Build Y axis config with optional symmetric bounds
    y_axis = {
        "ticks": {"color": axis, "stepSize": (1.0 if p.mode == "kg" else None)},
        "grid": {"color": grid},
        "title": {"display": True, "text": ("Вес (кг)" if p.mode == "kg" else "Вес (%)"), "color": axis, "font": {"size": 16}},
    }
    if y_min is not None:
        y_axis["min"] = y_min
        y_axis["max"] = y_max

    options = {
        "responsive": False,
        "plugins": {
            "legend": {"display": False},
            "title": {"display": True, "text": "План достижения цели", "color": "#e2e8f0", "font": {"size": 18}},
            "tooltip": {"enabled": False},
        },
        "scales": {
            "x": {"ticks": {"color": axis}, "grid": {"color": grid}, "title": {"display": True, "text": "Дата", "color": axis, "font": {"size": 16}}},
            "y": y_axis,
        },
    }

    # Disable datalabels globally; enabled only on main dataset above
    if getattr(settings, "CHARTS_USE_DATALABELS", True):
        options.setdefault("plugins", {})["datalabels"] = {"display": False}

    if p.target_value is not None:
        # Add target as extra dataset using stepped line effect via scriptable borderDash not supported here; use plugin annotation? Simplify: extra dataset constant line
        datasets.append({
            "label": "Цель",
            "data": [p.target_value for _ in p.labels],
            "borderColor": target,
            "borderDash": [6, 6],
            "pointRadius": 0,
            "tension": 0,
            "borderWidth": 2,
            "datalabels": {"display": False},
        })

    return {
        "type": "line",
        "data": {"labels": p.labels, "datasets": datasets},
        "options": options,
        "backgroundColor": bg,
    }


def _config_cache_key(user_id: int, payload_hash: str, *args, **kwargs) -> str:
    # Cache key only depends on user and the provided payload hash
    return build_key(user_id, payload_hash)


@cached(ttl=300, namespace="charts", key_builder=_config_cache_key)
async def get_plan_chart_png(user_id: int, payload_hash: str, *,
                             start_weight: float, goal_weight: Optional[float], weekly_rate: float,
                             start_date: date, eta_date: Optional[date]) -> Optional[bytes]:
    logger.info(
        "charts.invoke | user_id={} | enabled={} | provider={} | mode={} | weekly={} | start={} | eta={}",
        user_id,
        settings.CHARTS_ENABLED,
        settings.CHARTS_PROVIDER,
        settings.CHARTS_PRIVACY_MODE,
        weekly_rate,
        start_date,
        eta_date,
    )
    if not settings.CHARTS_ENABLED:
        logger.info("charts.skip | reason=disabled")
        return None
    if settings.CHARTS_PROVIDER != "quickchart":
        logger.info("charts.skip | reason=provider:{}", settings.CHARTS_PROVIDER)
        return None

    proj = build_projection(start_weight, goal_weight, weekly_rate, start_date, eta_date, mode=settings.CHARTS_PRIVACY_MODE)
    logger.info(
        "charts.projection_ready | points={} | target_present={} | mode={}",
        len(proj.labels),
        bool(proj.target_value is not None),
        proj.mode,
    )
    config = _chart_config_from_projection(proj)
    logger.info("charts.config_ready")

    url = settings.QUICKCHART_URL.rstrip("/")
    width = settings.QUICKCHART_WIDTH
    height = settings.QUICKCHART_HEIGHT

    # We'll POST for larger configs
    body = {
        "chart": config,
        "width": width,
        "height": height,
        "backgroundColor": getattr(settings, "CHARTS_COLOR_BG", None) or "#0b1220",
        "format": "png",
        "version": "4",
        "devicePixelRatio": 1.0,
    }
    _plugins = ["chartjs-plugin-annotation"]
    if getattr(settings, "CHARTS_USE_DATALABELS", True):
        _plugins.insert(0, "chartjs-plugin-datalabels")
    body["plugins"] = _plugins

    timeout = aiohttp.ClientTimeout(total=settings.QUICKCHART_TIMEOUT_SEC)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Try POST first
            try:
                async with session.post(url, json=body) as resp:
                    if resp.status != 200:
                        logger.warning("charts.quickchart_http_error_post | status={}", resp.status)
                    else:
                        data = await resp.read()
                        if data:
                            logger.info("charts.sent | user_id={} | bytes={} | method=POST", user_id, len(data))
                            return data
            except asyncio.TimeoutError:
                logger.warning("charts.quickchart_timeout_post | user_id={} | timeout_s={}", user_id, settings.QUICKCHART_TIMEOUT_SEC)
            except Exception as e:
                logger.warning("charts.quickchart_exception_post | user_id={} | err={}", user_id, e or type(e).__name__)

            # Fallback to GET with encoded config
            cfg_str = json.dumps(config, separators=(",", ":"), ensure_ascii=False)
            params = {
                "c": cfg_str,
                "width": str(width),
                "height": str(height),
                "backgroundColor": getattr(settings, "CHARTS_COLOR_BG", None) or "#0b1220",
                "format": "png",
                "version": "4",
                "devicePixelRatio": "1.0",
            }
            _pl = ["chartjs-plugin-annotation"]
            if getattr(settings, "CHARTS_USE_DATALABELS", True):
                _pl.insert(0, "chartjs-plugin-datalabels")
            params["plugins"] = ",".join(_pl)
            # Important: do not mark spaces as safe, fully encode
            get_url = url + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
            try:
                async with session.get(get_url) as resp2:
                    if resp2.status != 200:
                        logger.warning("charts.quickchart_http_error_get | status={}", resp2.status)
                        return None
                    data = await resp2.read()
                    logger.info("charts.sent | user_id={} | bytes={} | method=GET", user_id, len(data) if data else 0)
                    return data
            except asyncio.TimeoutError:
                logger.warning("charts.quickchart_timeout_get | user_id={} | timeout_s={}", user_id, settings.QUICKCHART_TIMEOUT_SEC)
                return None
            except Exception as e:
                logger.warning("charts.quickchart_exception_get | user_id={} | err={}", user_id, e or type(e).__name__)
                return None
    except Exception as e:
        logger.warning("charts.quickchart_session_exception | user_id={} | err={}", user_id, e or type(e).__name__)
        return None


def _history_panel_config(title: str, labels: List[str], values: List[int], *,
                         bar_color: str, norm_value: Optional[float]) -> dict:
    bg = getattr(settings, "CHARTS_COLOR_BG", None) or "#0b1220"
    grid = getattr(settings, "CHARTS_COLOR_GRID", None) or "#203049"
    axis = getattr(settings, "CHARTS_COLOR_AXIS", None) or "#94a3b8"

    # Base bar dataset
    # Convert solid hex color to rgba with 80% opacity for bar fill
    bar_bg = bar_color
    try:
        hc = (bar_color or "").lstrip("#")
        if len(hc) == 3:
            r = int(hc[0] * 2, 16); g = int(hc[1] * 2, 16); b = int(hc[2] * 2, 16)
            bar_bg = f"rgba({r},{g},{b},0.8)"
        elif len(hc) >= 6:
            r = int(hc[0:2], 16); g = int(hc[2:4], 16); b = int(hc[4:6], 16)
            bar_bg = f"rgba({r},{g},{b},0.8)"
    except Exception:
        pass
    datasets = [
        {
            "type": "bar",
            "label": title,
            "data": values,
            "backgroundColor": bar_bg,
            "borderColor": "#ffffff",
            "borderWidth": 2,
            "datalabels": {
                "display": True,
                "anchor": "end",
                "align": "end",
                "color": getattr(settings, "CHARTS_COLOR_LABEL_FG", None) or "#1e293b",
                "backgroundColor": getattr(settings, "CHARTS_COLOR_LABEL_BG", None) or "#ffffff",
                "borderRadius": 4,
                "padding": {"left": 8, "right": 8, "top": 4, "bottom": 4},
                "font": {"weight": "700", "size": 20},
            },
        }
    ]

    # Dashed norm line
    if norm_value is not None and float(norm_value) > 0:
        datasets.append({
            "type": "line",
            "label": "Норма",
            "data": [float(norm_value) for _ in labels],
            "borderColor": "#ffffff",
            "borderDash": [6, 6],
            "pointRadius": 0,
            "tension": 0,
            "datalabels": {"display": False},
        })

    # Annotation for single centered label "Норма: N"
    annotations: dict = {}
    if norm_value is not None and float(norm_value) > 0 and labels:
        mid = max(0, min(len(labels) - 1, len(labels) // 2))
        label_text = f"Норма: {int(round(float(norm_value)))}"
        annotations["normLabel"] = {
            "type": "label",
            "xValue": labels[mid],
            "yValue": float(norm_value),
            "content": label_text,
            "backgroundColor": "rgba(0,0,0,0)",
            "color": "#e2e8f0",
            "font": {"size": 22, "weight": "700"},
            "yAdjust": -18,
            "textAlign": "center",
        }

    cfg = {
        "type": "bar",
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "responsive": False,
            "plugins": {
                "legend": {"display": False},
                "title": {"display": True, "text": title, "color": "#ffffff", "font": {"size": 24}},
                "tooltip": {"enabled": False},
                "datalabels": {
                    "display": "function(ctx){var ds=ctx && ctx.dataset || {}; return ds && ds.type === 'bar';}"
                },
                "annotation": {"annotations": annotations},
            },
            "scales": {
                "x": {"ticks": {"color": axis, "font": {"size": 16}}, "grid": {"color": grid}},
                "y": {"ticks": {"color": "#ffffff", "font": {"size": 16}}, "grid": {"color": grid}},
            },
        },
        "backgroundColor": bg,
    }
    return cfg


async def _render_quickchart(config: dict, *, width: int, height: int) -> Optional[bytes]:
    if settings.CHARTS_PROVIDER != "quickchart":
        logger.info("charts.skip | reason=provider:{}", settings.CHARTS_PROVIDER)
        return None
    url = settings.QUICKCHART_URL.rstrip("/")
    body = {
        "chart": config,
        "width": width,
        "height": height,
        "backgroundColor": getattr(settings, "CHARTS_COLOR_BG", None) or "#0b1220",
        "format": "png",
        "version": "4",
        "devicePixelRatio": 1.0,
    }
    if getattr(settings, "CHARTS_USE_DATALABELS", True):
        body["plugins"] = ["chartjs-plugin-datalabels"]
    timeout = aiohttp.ClientTimeout(total=settings.QUICKCHART_TIMEOUT_SEC)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.post(url, json=body) as resp:
                    if resp.status == 200:
                        return await resp.read()
            except asyncio.TimeoutError:
                logger.warning("charts.quickchart_timeout_post | timeout_s={}", settings.QUICKCHART_TIMEOUT_SEC)
            except Exception as e:
                logger.warning("charts.quickchart_exception_post | err={}", e or type(e).__name__)
            import json as _json
            import urllib.parse as _u
            cfg_str = _json.dumps(config, separators=(",", ":"), ensure_ascii=False)
            params = {
                "c": cfg_str,
                "width": str(width),
                "height": str(height),
                "backgroundColor": getattr(settings, "CHARTS_COLOR_BG", None) or "#0b1220",
                "format": "png",
                "version": "4",
                "devicePixelRatio": "1.0",
            }
            if getattr(settings, "CHARTS_USE_DATALABELS", True):
                params["plugins"] = "chartjs-plugin-datalabels"
            get_url = url + "?" + _u.urlencode(params, quote_via=_u.quote)
            try:
                async with session.get(get_url) as resp2:
                    if resp2.status != 200:
                        return None
                    return await resp2.read()
            except asyncio.TimeoutError:
                logger.warning("charts.quickchart_timeout_get | timeout_s={}", settings.QUICKCHART_TIMEOUT_SEC)
                return None
            except Exception as e:
                logger.warning("charts.quickchart_exception_get | err={}", e or type(e).__name__)
                return None
    except Exception as e:
        logger.warning("charts.quickchart_session_exception | err={}", e or type(e).__name__)
        return None


@cached(ttl=300, namespace="charts", key_builder=_config_cache_key)
async def get_history_chart_png(user_id: int, payload_hash: str, *,
                                x_labels: List[str], cal: List[int], p: List[int], f: List[int], c: List[int],
                                norms: Optional[dict]) -> Optional[bytes]:
    if not settings.CHARTS_ENABLED:
        logger.info("charts.skip | reason=disabled")
        return None
    if not _PIL_OK:
        logger.warning("charts.history_png_pillow_missing | user_id={} | returning None (install Pillow)", user_id)
        return None
    w_total, h_total = 1920, 1440
    margin_lr = 40
    margin_tb = 40
    title_h = 0
    gap = 20
    panel_w = int((w_total - margin_lr * 2 - gap) / 2)
    panel_h = int((h_total - margin_tb - title_h - gap - margin_tb - gap) / 2)

    titles = ["Калории", "Белки (г)", "Жиры (г)", "Углеводы (г)"]
    colors = ["#FF8FAB", "#FF9770", "#FFD95C", "#96E072"]
    values = [cal, p, f, c]
    norms_seq = [
        (norms or {}).get("cal"),
        (norms or {}).get("p"),
        (norms or {}).get("f"),
        (norms or {}).get("c"),
    ]

    panels: List[Optional[bytes]] = []
    for i in range(4):
        cfg = _history_panel_config(titles[i], x_labels, values[i], bar_color=colors[i], norm_value=norms_seq[i])
        png = await _render_quickchart(cfg, width=panel_w, height=panel_h)
        panels.append(png)

    try:
        bg_color_hex = getattr(settings, "CHARTS_COLOR_BG", None) or "#0b1220"
        if bg_color_hex.startswith("#") and len(bg_color_hex) in {4, 7}:
            if len(bg_color_hex) == 4:
                r = int(bg_color_hex[1] * 2, 16); g = int(bg_color_hex[2] * 2, 16); b = int(bg_color_hex[3] * 2, 16)
            else:
                r = int(bg_color_hex[1:3], 16); g = int(bg_color_hex[3:5], 16); b = int(bg_color_hex[5:7], 16)
            bg_rgb = (r, g, b, 255)
        else:
            bg_rgb = (11, 18, 32, 255)
        canvas = Image.new("RGBA", (w_total, h_total), bg_rgb)

        positions = [
            (margin_lr, margin_tb + title_h + gap),
            (margin_lr + panel_w + gap, margin_tb + title_h + gap),
            (margin_lr, margin_tb + title_h + gap + panel_h + gap),
            (margin_lr + panel_w + gap, margin_tb + title_h + gap + panel_h + gap),
        ]
        for idx, png in enumerate(panels):
            if not png:
                continue
            try:
                img = Image.open(__import__("io").BytesIO(png)).convert("RGBA")
                canvas.paste(img, positions[idx])
            except Exception:
                continue
        out = __import__("io").BytesIO()
        canvas.save(out, format="PNG")
        return out.getvalue()
    except Exception as e:
        logger.warning("charts.history_png_compose_failed | err={}", e or type(e).__name__)
        return None
