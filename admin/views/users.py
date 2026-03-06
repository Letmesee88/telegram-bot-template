# ruff: noqa: RUF012
from admin.views.base import RoleView


class UserView(RoleView):
    can_delete = True
    can_create = False
    can_edit = True
    can_view_details = True
    edit_modal = True
    can_export = True
    details_modal = True
    export_types = ["csv", "xlsx", "json", "yaml"]

    column_searchable_list = ["id", "username", "first_name", "last_name", "email"]
    column_filters = ["email", "is_admin", "is_suspicious", "is_block", "is_premium", "created_at"]
    column_list = [
        "id",
        "username",
        "first_name",
        "last_name",
        "email",
        "language_code",
        "is_admin",
        "is_suspicious",
        "is_block",
        "is_premium",
        "created_at",
    ]
    column_default_sort = ("created_at", True)
