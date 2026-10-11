"""Allowlisted builders for the documented Things URL scheme."""

import json
from urllib.parse import quote, urlencode

from .models import ProjectTemplate, URLFields


def build_url(
    command: str, fields: URLFields, item_id: str | None = None, token: str | None = None
) -> str:
    if command not in ("add", "add-project", "update", "update-project"):
        raise ValueError("unsupported URL command")
    update = command.startswith("update")
    project = command.endswith("project")
    values = fields.model_dump(exclude_none=True)
    if not values:
        raise ValueError("provide at least one URL field")
    update_only = {
        "prepend_notes",
        "append_notes",
        "add_tags",
        "prepend_checklist_items",
        "append_checklist_items",
        "duplicate",
    }
    if not update and update_only & values.keys():
        raise ValueError("update-only fields cannot be used for creation")
    if project and (
        any("checklist" in key for key in values)
        or {"list_id", "heading", "heading_id"} & values.keys()
    ):
        raise ValueError("projects do not accept task containers, headings, or checklists")
    if not project and "area_id" in values:
        raise ValueError("use list_id for a task's project or area")
    params = {}
    if update:
        if not item_id:
            raise ValueError("item_id is required for updates")
        if not token:
            raise ValueError("Set THINGS_URL_AUTH_TOKEN in the server environment for URL updates.")
        params.update({"id": item_id, "auth-token": token})
    elif item_id:
        raise ValueError("item_id is only valid for updates")
    for key, value in values.items():
        if isinstance(value, list):
            value = ("," if key in ("tags", "add_tags") else "\n").join(value)
        elif isinstance(value, bool):
            value = str(value).lower()
        params[key.replace("_", "-")] = value
    return encode(command, params)


def encode(command: str, params: dict) -> str:
    # Things percent-decodes values; form encoding would turn spaces into literal '+'.
    result = "things:///" + command + "?" + urlencode(params, quote_via=quote, safe="")
    if len(result.encode()) > 60000:
        raise ValueError("URL exceeds the server's 60 KB limit; split the request")
    return result


def template_url(template: ProjectTemplate) -> str:
    attrs = template.model_dump(exclude_none=True, exclude={"items"})
    if "area_id" in attrs:
        attrs["area-id"] = attrs.pop("area_id")
    children = []
    for item in template.items:
        values = item.model_dump(exclude_none=True, exclude={"type"})
        if "checklist" in values:
            values["checklist-items"] = [
                {"type": "checklist-item", "attributes": row} for row in values.pop("checklist")
            ]
        children.append({"type": item.type, "attributes": values})
    attrs["items"] = children
    return encode(
        "json",
        {
            "data": json.dumps(
                [{"type": "project", "attributes": attrs}],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        },
    )
