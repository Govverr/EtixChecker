"""Domain enumerations."""

from enum import Enum


class ShowStatus(str, Enum):
    OK = "OK"
    SOLD_OUT = "SOLD OUT"
    ENDED = "ENDED"
    INSUFFICIENT = "НЕДОСТАТОЧНО"
    PARTIAL = "ЧАСТИЧНО"
    BLOCKED = "BLOCKED"
    FAILED = "НЕ УДАЛОСЬ"
    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"


class ProfileRole(str, Enum):
    ACTIVE = "ACTIVE"
    RESERVE = "RESERVE"
    IN_USE = "IN_USE"
    BUSY_EXTERNAL = "BUSY_EXTERNAL"
    FAILED = "FAILED"
    DISABLED = "DISABLED"
