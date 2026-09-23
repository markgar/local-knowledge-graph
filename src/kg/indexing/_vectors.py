"""Canonical finite, normalized little-endian float32 projections."""

import math
import struct
from collections.abc import Sequence


class InvalidVector(ValueError):
    pass


def encode(vector: Sequence[float], dimensions: int) -> bytes:
    if len(vector) != dimensions or any(
        type(value) not in (int, float) or not math.isfinite(value) for value in vector
    ):
        raise InvalidVector()
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise InvalidVector()
    result = struct.pack(f"<{dimensions}f", *(value / norm for value in vector))
    validate(result, dimensions)
    return result


def validate(blob: bytes, dimensions: int) -> None:
    if type(blob) is not bytes or not 1 <= dimensions <= 1024 or len(blob) != dimensions * 4:
        raise InvalidVector()
    values = struct.unpack(f"<{dimensions}f", blob)
    if any(not math.isfinite(value) for value in values) or not math.isclose(
        math.hypot(*values),
        1.0,
        rel_tol=1e-6,
        abs_tol=1e-6,
    ):
        raise InvalidVector()
