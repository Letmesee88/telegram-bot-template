import asyncio
import sys
from typing import Any, Optional

# Import internal helpers only, no Telegram send
from bot.services.reports import (
    _fetch_plan_and_fact,
    _collect_user_context,
    _gen_llm_content,
)
from bot.core.config import settings


async def _safe_fetch(user_id: int, timeout: float = 2.0):
    try:
        return await asyncio.wait_for(_fetch_plan_and_fact(user_id), timeout=timeout)
    except Exception as e:
        print("WARN_DB_FETCH:", repr(e))
        plan = {"calories": 0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
        fact = {"calories": 0, "protein_g": 0.0, "fat_g": 0.0, "carbs_g": 0.0}
        y_local = None
        return plan, fact, y_local


async def _safe_ctx(user_id: int, timeout: float = 2.5) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(_collect_user_context(user_id), timeout=timeout)
    except Exception as e:
        print("WARN_DB_CTX:", repr(e))
        return {}


async def main():
    if len(sys.argv) < 2:
        print("Usage: python _dev_smoke_daily_report.py <user_id>")
        return
    try:
        user_id = int(sys.argv[1])
    except Exception:
        print("Invalid user_id")
        return

    # Optional modes: 'relax' (looser lengths/timeouts), 'full' (bigger DB timeouts + context print)
    mode = sys.argv[2].strip().lower() if len(sys.argv) >= 3 else ''
    relax = (mode == 'relax')
    full = (mode == 'full')
    if relax:
        try:
            settings.DAILY_REPORTS_LLM_TIMEOUT_SEC = 15
            settings.DAILY_REPORTS_LLM_MAX_ATTEMPTS = 3
            settings.DAILY_REPORTS_TARGET_LEN_MOTIVATION_MIN = 280
            settings.DAILY_REPORTS_TARGET_LEN_MOTIVATION_MAX = 380
            settings.DAILY_REPORTS_TARGET_LEN_ADVICE_MIN = 180
            settings.DAILY_REPORTS_TARGET_LEN_ADVICE_MAX = 340
        except Exception:
            pass

    fetch_to = 12.0 if full else 2.0
    ctx_to = 12.0 if full else 2.5
    plan, fact, _ = await _safe_fetch(user_id, timeout=fetch_to)
    ctx = await _safe_ctx(user_id, timeout=ctx_to)

    if full:
        try:
            print("PLAN:", plan)
            print("FACT:", fact)
            keys = [
                "current_weight","goal_weight","start_weight","progress_pct",
                "trend_7d","adherence_streak_days","logging_days_last7"
            ]
            ctx_view = {k: ctx.get(k) for k in keys}
            print("CTX:", ctx_view)
        except Exception:
            pass

    mot, adv, err = await _gen_llm_content(plan, fact, ctx)
    print("ERR:", err)
    print("MOT_LEN:", len(mot or ""))
    print("ADV_LEN:", len(adv or ""))
    print("MOT_SAMPLE:", (mot or "")[:500].replace("\n", " "))
    print("ADV_SAMPLE:", (adv or "")[:500].replace("\n", " "))
    print("OK_NO_FALLBACK:", err is None)


if __name__ == "__main__":
    asyncio.run(main())
