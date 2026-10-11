"""Strict input contracts shared by the MCP tools and automation backend."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(max_length=4000, pattern=r"^[^\x00]*$")]
Title = Annotated[str, StringConstraints(min_length=1, max_length=4000, pattern=r"^[^\x00]*$")]
Notes = Annotated[str, StringConstraints(max_length=10000, pattern=r"^[^\x00]*$")]
ID = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"^[^\x00]*$")]
TagName = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^,\r\n\x00]+$")
]
Kind = Literal["to-do", "project", "area", "tag"]
Status = Literal["open", "completed", "canceled"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(Model):
    kind: Literal["list", "project", "area"]
    id: ID


class Query(Model):
    kind: Literal["to-do", "project", "area", "tag", "list", "selected"] = "to-do"
    container: Target | None = None
    text: Text | None = None
    status: Status | None = None
    tag: TagName | None = None
    offset: int = Field(default=0, ge=0, le=1000000)
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def valid_filters(self):
        if self.container and self.kind != "to-do":
            raise ValueError("container requires kind=to-do")
        if (self.status or self.tag) and self.kind not in ("to-do", "project", "selected"):
            raise ValueError("status and tag filters require tasks or projects")
        return self


class Fields(Model):
    title: Title | None = None
    notes: Notes | None = None
    tags: list[TagName] | None = Field(default=None, max_length=100)
    deadline: date | Literal[""] | None = None
    status: Status | None = None
    parent_tag_id: ID | Literal[""] | None = None
    keyboard_shortcut: Annotated[str, StringConstraints(max_length=1)] | None = None

    def check_kind(self, kind: Kind, *, creating: bool = False):
        values = self.model_dump(exclude_none=True)
        if creating and not self.title:
            raise ValueError("title is required for creation")
        allowed = {"title"}
        if kind in ("to-do", "project"):
            allowed |= {"notes", "tags", "deadline", "status"}
        elif kind == "area":
            allowed |= {"tags"}
        elif kind == "tag":
            allowed |= {"parent_tag_id", "keyboard_shortcut"}
            if self.title and any(c in self.title for c in ",\r\n"):
                raise ValueError("tag titles cannot contain commas or newlines")
        if values.keys() - allowed:
            raise ValueError(f"unsupported fields for {kind}: {sorted(values.keys() - allowed)}")
        if not values:
            raise ValueError("provide at least one field")
        return self


class URLFields(Model):
    """Public URL parameters. None omits a field; empty text clears supported fields."""

    title: Text | None = None
    notes: Notes | None = None
    prepend_notes: Notes | None = None
    append_notes: Notes | None = None
    when: Text | None = None
    deadline: Text | None = None
    tags: list[TagName] | None = Field(default=None, max_length=100)
    add_tags: list[TagName] | None = Field(default=None, max_length=100)
    checklist_items: list[Text] | None = Field(default=None, max_length=100)
    prepend_checklist_items: list[Text] | None = Field(default=None, max_length=100)
    append_checklist_items: list[Text] | None = Field(default=None, max_length=100)
    list_id: ID | None = None
    area_id: ID | None = None
    heading_id: ID | None = None
    heading: Text | None = None
    completed: bool | None = None
    canceled: bool | None = None
    duplicate: bool | None = None
    reveal: bool | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.completed and self.canceled:
            raise ValueError("completed and canceled cannot both be true")
        if self.heading and self.heading_id:
            raise ValueError("use heading or heading_id, not both")
        for key in ("checklist_items", "prepend_checklist_items", "append_checklist_items"):
            rows = getattr(self, key)
            if rows and any("\n" in row or "\r" in row for row in rows):
                raise ValueError("each checklist entry must be one line")
            if rows and len("\n".join(rows)) > 4000:
                raise ValueError("combined checklist text exceeds 4000 characters")
        for key in ("tags", "add_tags"):
            if getattr(self, key) and len(",".join(getattr(self, key))) > 4000:
                raise ValueError("combined tag text exceeds 4000 characters")
        return self


class ChecklistItem(Model):
    title: Text
    completed: bool = False
    canceled: bool = False

    @model_validator(mode="after")
    def valid_status(self):
        if self.completed and self.canceled:
            raise ValueError("completed and canceled cannot both be true")
        return self


class TemplateTask(Model):
    type: Literal["to-do"] = "to-do"
    title: Title
    notes: Notes | None = None
    when: Text | None = None
    deadline: Text | None = None
    tags: list[TagName] | None = Field(default=None, max_length=100)
    checklist: list[ChecklistItem] | None = Field(default=None, max_length=100)


class TemplateHeading(Model):
    type: Literal["heading"] = "heading"
    title: Title


class ProjectTemplate(Model):
    title: Title
    notes: Notes | None = None
    when: Text | None = None
    deadline: Text | None = None
    area_id: ID | None = None
    tags: list[TagName] | None = Field(default=None, max_length=100)
    items: list[Annotated[TemplateTask | TemplateHeading, Field(discriminator="type")]] = Field(
        default_factory=list, max_length=99
    )
