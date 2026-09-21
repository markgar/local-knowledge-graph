"""Conservative logical JSON sizing before validation, copying or serialization."""

from datetime import datetime

from pydantic import BaseModel

REPORT_BYTES = 256 << 10
TOTAL_BYTES = 8 << 20
DEPENDENCY_BYTES = 64 << 10
PAYLOAD_BYTES = REPORT_BYTES - DEPENDENCY_BYTES
MAX_REPORTS = 32
RETENTION_SECONDS = 300


def bounded_size(value: object, limit: int) -> int:
    """Upper bound compact UTF-8 JSON, short-circuiting without building a dump.

    Count JSON string escapes without allocating an encoded string. ASCII JSON
    sizing also bounds the compact Unicode encoding used by Pydantic.
    """
    if limit < 0:
        return 1
    if value is None or type(value) is bool:
        return 5
    if isinstance(value, str):
        if len(value) > limit:
            return limit + 1
        size = 2
        for char in value:
            code = ord(char)
            size += (
                2
                if char in '"\\\b\f\n\r\t'
                else 6
                if code < 32 or 127 < code <= 65535
                else 12
                if code > 65535
                else 1
            )
            if size > limit:
                return limit + 1
        return size
    if type(value) is int:
        return 2 + value.bit_length()
    if type(value) is float:
        return 32
    if isinstance(value, datetime):
        return 64
    if isinstance(value, BaseModel):
        total = 2
        if value.__pydantic_extra__:
            return limit + 1
        for key, item in value.__dict__.items():
            if key not in type(value).model_fields:
                return limit + 1
            total += bounded_size(key, limit - total) + 2
            total += bounded_size(item, limit - total)
            if total > limit:
                return limit + 1
        return total
    if isinstance(value, (tuple, list)):
        if len(value) > 200:
            return limit + 1
        total = 2
        for item in value:
            total += 1 + bounded_size(item, limit - total)
            if total > limit:
                return limit + 1
        return total
    return limit + 1
