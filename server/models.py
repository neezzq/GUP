from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class Launcher(StrEnum):
    STEAM = "steam"
    EPIC = "epic"
    RIOT = "riot"
    BATTLENET = "battlenet"
    VKPLAY = "vkplay"
    EA = "ea"
    LESTA = "lesta"


LAUNCHER_NAMES = dict(zip(Launcher, (
    "Steam", "Epic Games Store", "Riot Client", "Battle.net", "VK Play",
    "EA App", "Lesta Game Center",
), strict=True))


class ReportStatus(StrEnum):
    STARTED = "started"
    UPDATED = "updated"
    CURRENT = "current"
    ERROR = "error"
    LOGIN_REQUIRED = "login_required"
    CONFIRMATION = "confirmation_required"
    BUSY = "busy"
    SKIPPED = "skipped"
    UNVERIFIED = "unverified"


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]
Version = Annotated[str, StringConstraints(pattern=r"^[0-9]+(?:\.[0-9]+){1,3}$", max_length=25)]


class Heartbeat(Request):
    version: Version


class Registration(Heartbeat):
    key: Token
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class LeaseRequest(Request):
    launcher: Launcher


class LeaseAccess(LeaseRequest):
    lease: Token


class Report(LeaseRequest):
    status: ReportStatus
    updated: Annotated[int, Field(ge=0, le=100000)] = 0
    current: Annotated[int, Field(ge=0, le=100000)] = 0
