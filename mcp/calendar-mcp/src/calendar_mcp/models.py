"""Bounded read requests shared by the MCP and native process."""

from datetime import datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

ID = Annotated[str, StringConstraints(min_length=1, max_length=512, pattern=r"^[^\x00]*$")]


class EventQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    period: Literal["today", "tomorrow", "this_week", "next_week"] | None = None
    time_zone: str | None = Field(default=None, max_length=128)
    calendar_ids: list[ID] | None = Field(default=None, min_length=1, max_length=100)
    text: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None
    include_notes: bool = False
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def date_range(self):
        if self.period is not None:
            if self.start is not None or self.end is not None:
                raise ValueError("Supply period or start and end, not both")
        elif self.start is None or self.end is None:
            raise ValueError("Supply period or both start and end")
        elif not timedelta(0) < self.end - self.start <= timedelta(days=93):
            raise ValueError("end must follow start by at most 93 days")
        if self.time_zone is not None:
            try:
                ZoneInfo(self.time_zone)
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError("time_zone must be an IANA timezone") from None
        if self.calendar_ids and len(self.calendar_ids) != len(set(self.calendar_ids)):
            raise ValueError("calendar_ids must not contain duplicates")
        return self

    def resolve(self, now: datetime, default_zone: str):
        """Resolve local calendar boundaries, including offset changes across DST."""
        zone = ZoneInfo(self.time_zone or default_zone)
        if self.period is None:
            return self
        day = now.astimezone(zone).date()
        days = 1
        if self.period == "tomorrow":
            day += timedelta(days=1)
        elif self.period in ("this_week", "next_week"):
            day -= timedelta(days=day.weekday())
            if self.period == "next_week":
                day += timedelta(days=7)
            days = 7
        return EventQuery.model_validate(
            self.model_dump()
            | {
                "period": None,
                "start": datetime.combine(day, time.min, zone),
                "end": datetime.combine(day + timedelta(days=days), time.min, zone),
                "time_zone": zone.key,
            }
        )
