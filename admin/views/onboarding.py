# ruff: noqa: RUF012
from __future__ import annotations

from datetime import datetime
from typing import Any

from markupsafe import Markup
from flask_admin.contrib.sqla import ModelView
from admin.views.base import RoleView


class OnboardingAnswerView(RoleView):
    can_delete = False
    can_create = False
    can_edit = False
    can_view_details = True
    details_modal = True
    can_export = True
    export_types = ["csv", "xlsx", "json", "yaml"]

    column_default_sort = ("created_at", True)

    # display helpers
    def _user_display(self, ctx: Any, model: Any, name: str) -> str:
        username = getattr(getattr(model, "user", None), "username", None)
        if username:
            return Markup(f'<a href="https://t.me/{username}" target="_blank">@{username}</a>')
        return str(model.user_id)

    column_formatters = {
        "user_display": _user_display,
    }

    column_list = [
        "id",
        "user_display",
        "user_id",
        "goal",
        "calories",
        "created_at",
    ]

    column_labels = {
        "user_display": "User",
    }

    column_filters = ["goal", "created_at"]
    column_searchable_list = ["user_id", "goal"]
