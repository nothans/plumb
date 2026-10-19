"""The fixture-shaped event document, validated as a whole before an import
touches the database.

Every text field goes through the same cleaners as the forms (control and
bidi characters refused, lengths capped, URLs http or https only, emails
well formed, times ISO 8601), so an imported team name cannot spoof a
certificate any more than a typed one can, and a malformed file is refused
with a message instead of failing halfway with a 500. Unknown keys are
ignored, so a file another tool wrote can still be read.
"""

from __future__ import annotations

import math
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from . import domain

ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _id(value, field: str) -> str:
    if not isinstance(value, str) or not ID.match(value):
        raise ValueError(f"{field} must be 1 to 64 letters, digits or _ . : -")
    return value


def _clean(fn, *args, **kwargs):
    """Run a domain cleaner, turning its refusal into a validation error."""
    try:
        return fn(*args, **kwargs)
    except domain.Invalid as exc:
        raise ValueError(exc.message) from exc


class _Doc(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Event(_Doc):
    id: str
    name: str
    submissions_close: str

    @field_validator("id", mode="before")
    @classmethod
    def _v_id(cls, v):
        return _id(v, "event id")

    @field_validator("name", mode="before")
    @classmethod
    def _v_name(cls, v):
        return _clean(domain.clean_text, v, "event name", required=True, max_len=120)

    @field_validator("submissions_close", mode="before")
    @classmethod
    def _v_close(cls, v):
        return _clean(domain.clean_ts, v, "submissions_close", required=True)


class Track(_Doc):
    id: str
    name: str

    @field_validator("id", mode="before")
    @classmethod
    def _v_id(cls, v):
        return _id(v, "track id")

    @field_validator("name", mode="before")
    @classmethod
    def _v_name(cls, v):
        return _clean(domain.clean_text, v, "track name", required=True, max_len=80)


class Judge(_Doc):
    id: str
    name: str = ""
    email: str
    tracks: list[str] = Field(default_factory=list)

    @field_validator("id", mode="before")
    @classmethod
    def _v_id(cls, v):
        return _id(v, "judge id")

    @field_validator("name", mode="before")
    @classmethod
    def _v_name(cls, v):
        return _clean(domain.clean_text, v, "judge name", max_len=100)

    @field_validator("email", mode="before")
    @classmethod
    def _v_email(cls, v):
        return _clean(domain.clean_email, v)

    @field_validator("tracks", mode="before")
    @classmethod
    def _v_tracks(cls, v):
        if not isinstance(v, list):
            raise ValueError("tracks must be a list of track ids")
        return [_id(t, "track id") for t in v]


class Team(_Doc):
    id: str
    name: str
    members: list[str] = Field(default_factory=list)

    @field_validator("id", mode="before")
    @classmethod
    def _v_id(cls, v):
        return _id(v, "team id")

    @field_validator("name", mode="before")
    @classmethod
    def _v_name(cls, v):
        return _clean(domain.clean_text, v, "team name", required=True, max_len=80)

    @field_validator("members", mode="before")
    @classmethod
    def _v_members(cls, v):
        if not isinstance(v, list):
            raise ValueError("members must be a list of email addresses")
        return [_clean(domain.clean_email, m) for m in v]


class Project(_Doc):
    id: str
    team: str
    track: str | None = None
    title: str
    summary: str = ""
    description: str = ""
    repo_url: str = ""
    demo_url: str = ""
    status: str | None = None
    disqualified_reason: str | None = None
    submitted_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @field_validator("id", "team", mode="before")
    @classmethod
    def _v_ids(cls, v, info):
        return _id(v, f"project {info.field_name}")

    @field_validator("track", mode="before")
    @classmethod
    def _v_track(cls, v):
        return None if v in (None, "") else _id(v, "project track")

    @field_validator("title", mode="before")
    @classmethod
    def _v_title(cls, v):
        return _clean(domain.clean_text, v, "project title", required=True, max_len=120)

    @field_validator("summary", mode="before")
    @classmethod
    def _v_summary(cls, v):
        return _clean(domain.clean_text, v, "project summary", max_len=280)

    @field_validator("description", mode="before")
    @classmethod
    def _v_description(cls, v):
        return _clean(domain.clean_text, v, "project description", max_len=10000)

    @field_validator("repo_url", "demo_url", mode="before")
    @classmethod
    def _v_url(cls, v, info):
        return _clean(domain.clean_url, v, info.field_name)

    @field_validator("status", mode="before")
    @classmethod
    def _v_status(cls, v):
        if v in (None, ""):
            return None
        if v not in ("draft", "submitted"):
            raise ValueError("status must be draft or submitted")
        return v

    @field_validator("disqualified_reason", mode="before")
    @classmethod
    def _v_reason(cls, v):
        return _clean(domain.clean_text, v, "disqualified_reason", max_len=300) or None

    @field_validator("submitted_at", "created_at", "updated_at", mode="before")
    @classmethod
    def _v_ts(cls, v, info):
        return _clean(domain.clean_ts, v, info.field_name)


class Score(_Doc):
    judge: str
    project: str
    criteria: dict[str, int]
    comment: str = ""
    submitted_at: str | None = None
    updated_at: str | None = None

    @field_validator("judge", "project", mode="before")
    @classmethod
    def _v_ids(cls, v, info):
        return _id(v, f"score {info.field_name}")

    @field_validator("criteria", mode="before")
    @classmethod
    def _v_criteria(cls, v):
        if not isinstance(v, dict) or not v:
            raise ValueError("criteria must be an object of criterion key to whole-number score")
        out = {}
        for key, value in v.items():
            _id(key, "criterion key")
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"the score for {key} must be a whole number")
            out[key] = value
        return out

    @field_validator("comment", mode="before")
    @classmethod
    def _v_comment(cls, v):
        return _clean(domain.clean_text, v, "score comment", max_len=4000)

    @field_validator("submitted_at", "updated_at", mode="before")
    @classmethod
    def _v_ts(cls, v, info):
        return _clean(domain.clean_ts, v, info.field_name)


class Criterion(_Doc):
    key: str
    name: str = ""
    description: str = ""
    weight: float = 1.0
    min: int = 1
    max: int = 5

    @field_validator("key", mode="before")
    @classmethod
    def _v_key(cls, v):
        return _id(v, "criterion key")

    @field_validator("name", mode="before")
    @classmethod
    def _v_name(cls, v):
        return _clean(domain.clean_text, v, "criterion name", max_len=60)

    @field_validator("description", mode="before")
    @classmethod
    def _v_desc(cls, v):
        return _clean(domain.clean_text, v, "criterion description", max_len=300)

    @field_validator("weight", mode="before")
    @classmethod
    def _v_weight(cls, v):
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or not 0 <= v <= 100:
            raise ValueError("a criterion weight must be a number from 0 to 100")
        return float(v)

    @field_validator("min", "max", mode="before")
    @classmethod
    def _v_bounds(cls, v, info):
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"criterion {info.field_name} must be a whole number")
        return v


class Prize(_Doc):
    name: str
    description: str = ""
    track: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _v_name(cls, v):
        return _clean(domain.clean_text, v, "prize name", required=True, max_len=120)

    @field_validator("description", mode="before")
    @classmethod
    def _v_desc(cls, v):
        return _clean(domain.clean_text, v, "prize description", max_len=500)

    @field_validator("track", mode="before")
    @classmethod
    def _v_track(cls, v):
        return None if v in (None, "") else _id(v, "prize track")


class Assignment(_Doc):
    judge: str
    project: str
    source: str = "import"

    @field_validator("judge", "project", mode="before")
    @classmethod
    def _v_ids(cls, v, info):
        return _id(v, f"assignment {info.field_name}")

    @field_validator("source", mode="before")
    @classmethod
    def _v_source(cls, v):
        if v not in ("import", "auto", "manual"):
            raise ValueError("assignment source must be import, auto or manual")
        return v


class Comparison(_Doc):
    judge: str
    a: str
    b: str
    winner: str
    at: str | None = None

    @field_validator("judge", "a", "b", "winner", mode="before")
    @classmethod
    def _v_ids(cls, v, info):
        return _id(v, f"comparison {info.field_name}")

    @field_validator("at", mode="before")
    @classmethod
    def _v_ts(cls, v):
        return _clean(domain.clean_ts, v, "comparison at")


class Duplicate(_Doc):
    project: str
    duplicate_of: str

    @field_validator("project", "duplicate_of", mode="before")
    @classmethod
    def _v_ids(cls, v, info):
        return _id(v, f"duplicate {info.field_name}")


class PlumbBlock(_Doc):
    event: dict = Field(default_factory=dict)
    criteria: list[Criterion] = Field(default_factory=list)
    prizes: list[Prize] = Field(default_factory=list)
    assignments: list[Assignment] = Field(default_factory=list)
    comparisons: list[Comparison] = Field(default_factory=list)
    duplicates: list[Duplicate] = Field(default_factory=list)

    @field_validator("criteria")
    @classmethod
    def _v_criteria(cls, v):
        for c in v:
            if c.min >= c.max:
                raise ValueError(f"criterion {c.key}: min must be below max")
        if len({c.key for c in v}) != len(v):
            raise ValueError("criterion keys must be unique")
        return v


class Document(_Doc):
    event: Event
    tracks: list[Track] = Field(default_factory=list)
    judges: list[Judge] = Field(default_factory=list)
    teams: list[Team] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    scores: list[Score] = Field(default_factory=list)
    plumb: PlumbBlock = Field(default_factory=PlumbBlock)


def validate(data) -> dict:
    """The document, cleaned, as plain data; or domain.Invalid naming what is wrong."""
    if not isinstance(data, dict):
        raise domain.Invalid("expected a JSON object")
    try:
        doc = Document.model_validate(data)
    except ValidationError as exc:
        problems = []
        for e in exc.errors()[:10]:
            where = ".".join(str(p) for p in e["loc"])
            msg = e["msg"].removeprefix("Value error, ")
            problems.append(f"{where}: {msg}")
        more = "; ..." if len(exc.errors()) > 10 else ""
        raise domain.Invalid("the file has problems: " + "; ".join(problems) + more) from exc
    return doc.model_dump()
