import pytest

from bot.services.charts import _history_panel_config


def test_history_panel_with_norm_annotation_and_bar_styles():
    labels = ["Пн 10.10", "Вт 11.10", "Ср 12.10"]
    values = [1500, 1600, 1700]
    norm = 1800
    cfg = _history_panel_config(
        title="Калории",
        labels=labels,
        values=values,
        bar_color="#FF8FAB",
        norm_value=norm,
    )

    assert cfg["type"] == "bar"
    assert cfg["data"]["labels"] == labels

    # Bar dataset styles
    bar_ds = next(ds for ds in cfg["data"]["datasets"] if ds.get("type") == "bar")
    bg = bar_ds.get("backgroundColor")
    assert isinstance(bg, str) and bg.startswith("rgba(") and bg.endswith(",0.8)")
    assert bar_ds.get("borderColor") == "#ffffff"
    assert bar_ds.get("borderWidth") == 2

    # Norm line dataset present
    norm_ds = next(ds for ds in cfg["data"]["datasets"] if ds.get("label") == "Норма")
    assert norm_ds.get("type") == "line"
    assert norm_ds.get("borderDash") == [6, 6]
    assert norm in [int(round(float(x))) for x in norm_ds.get("data", [])]

    # Annotation with single centered label "Норма: N"
    plugins = cfg["options"]["plugins"]
    assert "annotation" in plugins
    ann = plugins["annotation"].get("annotations") or {}
    assert "normLabel" in ann
    norm_label = ann["normLabel"]
    assert norm_label.get("type") == "label"
    assert norm_label.get("content") == f"Норма: {int(round(float(norm)))}"
    assert norm_label.get("yValue") == float(norm)
    assert norm_label.get("xValue") in labels

    # Datalabels plugin exists and intended for bars
    assert "datalabels" in plugins


def test_history_panel_without_norm_hides_line_and_annotation():
    labels = ["Пн 10.10", "Вт 11.10"]
    values = [1200, 1300]
    cfg = _history_panel_config(
        title="Калории",
        labels=labels,
        values=values,
        bar_color="#FF8FAB",
        norm_value=0,
    )
    ds_labels = [ds.get("label") for ds in cfg["data"]["datasets"]]
    assert "Норма" not in ds_labels
    ann = cfg["options"]["plugins"]["annotation"].get("annotations") or {}
    assert "normLabel" not in ann
