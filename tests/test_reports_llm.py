import asyncio
import json
import pytest

from bot.services import reports as r


def _txt(n: int, ch: str = "А") -> str:
    return (ch * n)


@pytest.mark.asyncio
async def test_llm_success(monkeypatch):
    mot_min, mot_max, adv_min, adv_max = r._len_limits()
    mot = _txt(max(mot_min, (mot_min + mot_max)//2), "М")
    adv = _txt(max(adv_min, (adv_min + adv_max)//2), "С")

    async def fake_openai(kind, payload):
        obj = {"motivation_full": mot, "advice": adv}
        return json.dumps(obj, ensure_ascii=False)

    monkeypatch.setattr(r, "_openai_request", fake_openai)
    out_mot, out_adv, err = await r._gen_llm_content(
        {"calories": 1800, "protein_g": 120.0, "fat_g": 60.0, "carbs_g": 200.0},
        {"calories": 1700, "protein_g": 100.0, "fat_g": 55.0, "carbs_g": 180.0},
        ctx=None,
    )
    assert err is None
    assert r._in_range(out_mot, mot_min, mot_max)
    assert r._in_range(out_adv, adv_min, adv_max)


@pytest.mark.asyncio
async def test_llm_json_extraction_with_prefix(monkeypatch):
    mot_min, mot_max, adv_min, adv_max = r._len_limits()
    mot = _txt(mot_min, "М")
    adv = _txt(adv_min, "С")

    async def fake_openai(kind, payload):
        raw = f"Answer: \n```json\n{json.dumps({'motivation_full': mot, 'advice': adv}, ensure_ascii=False)}\n```"
        return raw

    monkeypatch.setattr(r, "_openai_request", fake_openai)
    out_mot, out_adv, err = await r._gen_llm_content(
        {"calories": 1800, "protein_g": 120.0, "fat_g": 60.0, "carbs_g": 200.0},
        {"calories": 1700, "protein_g": 100.0, "fat_g": 55.0, "carbs_g": 180.0},
        ctx=None,
    )
    assert err is None
    assert r._in_range(out_mot, mot_min, mot_max)
    assert r._in_range(out_adv, adv_min, adv_max)


@pytest.mark.asyncio
async def test_llm_refine_length(monkeypatch):
    mot_min, mot_max, adv_min, adv_max = r._len_limits()
    too_long_mot = _txt(mot_max + 50, "М")
    good_adv = _txt((adv_min + adv_max)//2, "С")
    refined_mot = _txt((mot_min + mot_max)//2, "М")

    calls = {"n": 0}

    async def fake_openai(kind, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return json.dumps({"motivation_full": too_long_mot, "advice": good_adv}, ensure_ascii=False)
        return json.dumps({"motivation_full": refined_mot, "advice": good_adv}, ensure_ascii=False)

    monkeypatch.setattr(r, "_openai_request", fake_openai)
    out_mot, out_adv, err = await r._gen_llm_content(
        {"calories": 1800, "protein_g": 120.0, "fat_g": 60.0, "carbs_g": 200.0},
        {"calories": 1700, "protein_g": 100.0, "fat_g": 55.0, "carbs_g": 180.0},
        ctx=None,
    )
    assert err is None
    assert r._in_range(out_mot, mot_min, mot_max)
    assert r._in_range(out_adv, adv_min, adv_max)


def test_neutral_fallback_lengths():
    mot_min, mot_max, adv_min, adv_max = r._len_limits()
    nm = r._neutral_motivation()
    na = r._neutral_advice()
    assert r._in_range(nm, mot_min, mot_max)
    assert r._in_range(na, adv_min, adv_max)
