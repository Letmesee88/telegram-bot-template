from __future__ import annotations
from typing import TYPE_CHECKING

from flask import abort, redirect, request, url_for
from flask_admin.contrib.sqla import ModelView
from flask_login import current_user

if TYPE_CHECKING:
    from werkzeug.wrappers.response import Response


class RoleView(ModelView):
    can_delete = False
    can_edit = False
    can_create = False
    can_view_details = False
    edit_modal = True
    create_modal = True
    can_export = False
    details_modal = True

    def is_accessible(self) -> bool:
        if not current_user.is_active or not current_user.is_authenticated:
            return False
        return bool(current_user.has_role("superuser"))

    def _handle_view(self, _name: str, **_kwargs: dict) -> Response | None:
        if not self.is_accessible():
            if current_user.is_authenticated:
                abort(403)
            else:
                return redirect(url_for("security.login", next=request.url))
        return None
