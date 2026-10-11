import json
from urllib.parse import unquote, urlsplit

import pytest
from pydantic import ValidationError

from things_mcp.models import Fields, ProjectTemplate, Query, URLFields
from things_mcp.urls import build_url, encode, template_url


def things_parameters(url):
    """Things percent-decodes values but does not treat '+' as a space."""
    return {
        unquote(key): [unquote(value)]
        for key, value in (part.split("=", 1) for part in urlsplit(url).query.split("&"))
    }


def test_url_encoding_preserves_data_and_cannot_add_parameters():
    title = '日程 &auth-token=attacker\n"\\?x-success=https://evil.invalid'
    url = build_url("update", URLFields(title=title, deadline="", tags=[]), "task&id=x", "secret")
    params = things_parameters(url)
    assert params == {
        "title": [title],
        "deadline": [""],
        "tags": [""],
        "id": ["task&id=x"],
        "auth-token": ["secret"],
    }


@pytest.mark.parametrize("command", ["add", "add-project", "update", "update-project"])
def test_spaces_and_literal_pluses_survive_things_decoding(command):
    title = "Pack test bag + spare"
    notes = "First line\nSecond line — café & tea + 50% / %20"
    updating = command.startswith("update")
    url = build_url(
        command,
        URLFields(title=title, notes=notes),
        "task-id" if updating else None,
        "secret" if updating else None,
    )
    params = things_parameters(url)
    assert params["title"] == [title]
    assert params["notes"] == [notes]
    assert "Pack%20test%20bag%20%2B%20spare" in url


@pytest.mark.parametrize("command", ["show", "search"])
def test_ui_query_spaces_use_percent_encoding(command):
    url = encode(command, {"query": "café + tea"})
    assert things_parameters(url)["query"] == ["café + tea"]


@pytest.mark.parametrize(
    "command,fields,item_id,token",
    [
        ("update", {"when": "today"}, "id", None),
        ("add", {"append_notes": "x"}, None, None),
        ("add-project", {"checklist_items": ["x"]}, None, None),
        ("add", {"area_id": "x"}, None, None),
        ("add", {"title": "x"}, "id", None),
        ("delete", {"title": "x"}, None, None),
    ],
)
def test_url_rejects_invalid_operations(command, fields, item_id, token):
    with pytest.raises(ValueError):
        build_url(command, URLFields(**fields), item_id, token)


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "x", "auth_token": "secret"},
        {"completed": True, "canceled": True},
        {"tags": ["two,tags"]},
        {"checklist_items": ["two\nrows"]},
        {"notes": "x" * 10001},
        {"title": "a\0b"},
        {"checklist_items": ["x"] * 101},
    ],
)
def test_url_model_is_strict(fields):
    with pytest.raises(ValidationError):
        URLFields(**fields)


def test_core_kind_and_query_validation():
    with pytest.raises(ValueError):
        Fields(notes="no").check_kind("area")
    with pytest.raises(ValueError):
        Fields().check_kind("to-do", creating=True)
    with pytest.raises(ValidationError):
        Query(kind="tag", status="open")
    with pytest.raises(ValidationError):
        Query(limit=201)


def test_template_uses_documented_structure_and_preserves_order():
    template = ProjectTemplate.model_validate(
        {
            "title": "Test trip + spare",
            "area_id": "area1",
            "items": [
                {"type": "heading", "title": "Before departure"},
                {
                    "type": "to-do",
                    "title": "Pack test bag",
                    "notes": "First line\nSecond line — café & tea + spare",
                    "checklist": [{"title": "My passport + visa", "completed": True}],
                },
            ],
        }
    )
    params = things_parameters(template_url(template))
    data = json.loads(params["data"][0])
    assert data[0]["attributes"]["area-id"] == "area1"
    assert data[0]["attributes"]["title"] == "Test trip + spare"
    items = data[0]["attributes"]["items"]
    assert [item["type"] for item in items] == ["heading", "to-do"]
    assert items[0]["attributes"]["title"] == "Before departure"
    assert items[1]["attributes"]["title"] == "Pack test bag"
    assert items[1]["attributes"]["notes"] == "First line\nSecond line — café & tea + spare"
    assert items[1]["attributes"]["checklist-items"][0] == {
        "type": "checklist-item",
        "attributes": {
            "title": "My passport + visa",
            "completed": True,
            "canceled": False,
        },
    }


def test_server_url_limit():
    with pytest.raises(ValueError, match="60 KB"):
        template_url(
            ProjectTemplate.model_validate(
                {
                    "title": "Large",
                    "items": [{"type": "to-do", "title": "x" * 4000}] * 99,
                }
            )
        )
