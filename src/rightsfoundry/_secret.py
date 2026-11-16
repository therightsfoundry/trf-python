"""SEC-03: a container for the API key that never prints, pickles or leaks its value.

The raw key exists in exactly two places inside the SDK: inside a :class:`Secret`
instance and, for the duration of one ``send`` call, inside the ``Authorization``
header value handed to the transport. Everything else sees the fingerprint.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, NoReturn

__all__ = ["Secret", "fingerprint"]

_FP_SALT = "rightsfoundry-key-fp:"


def fingerprint(value: str) -> str:
    """First 8 hex chars of ``sha256("rightsfoundry-key-fp:" + value)``.

    Stable, non-reversible (32 bits of a hash of a 512-bit secret) and different
    from any server-side token hash. The portal shows the same value next to the
    key's public code so a log line can be matched to a key without revealing it.
    """
    return hashlib.sha256((_FP_SALT + value).encode("utf-8")).hexdigest()[:8]


class Secret:
    """An opaque, redacting wrapper around a secret string.

    >>> s = Secret("0" * 128)
    >>> s
    Secret('[REDACTED fp=...]')

    ``repr``, ``str`` and ``format`` show only the fingerprint, pickling raises
    :class:`TypeError`, and equality is constant-time.
    """

    __slots__ = ("_fp", "_value")

    _value: str
    _fp: str

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("Secret value must be a str")
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_fp", fingerprint(value))

    @property
    def fingerprint(self) -> str:
        """The 8-hex-character key fingerprint shown as ``[REDACTED fp=…]``."""
        return self._fp

    def redacted(self) -> str:
        return f"[REDACTED fp={self._fp}]"

    def __repr__(self) -> str:
        return f"Secret('{self.redacted()}')"

    def __str__(self) -> str:
        return self.redacted()

    def __format__(self, format_spec: str) -> str:
        return format(self.redacted(), format_spec)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("Secret is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("Secret is immutable")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return hmac.compare_digest(self._value.encode("utf-8"), other._value.encode("utf-8"))

    def __hash__(self) -> int:
        return hash(("rightsfoundry.Secret", self._fp))

    def __len__(self) -> int:
        return len(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    # A Secret must never be serialised: pickling, copyreg and shelve all go
    # through these hooks.
    def __reduce__(self) -> NoReturn:
        raise TypeError("rightsfoundry.Secret cannot be pickled or serialised")

    def __reduce_ex__(self, protocol: Any) -> NoReturn:
        raise TypeError("rightsfoundry.Secret cannot be pickled or serialised")

    def __getstate__(self) -> NoReturn:
        raise TypeError("rightsfoundry.Secret cannot be pickled or serialised")

    def __copy__(self) -> Secret:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Secret:
        return self


def _reveal(secret: Secret) -> str:
    """Package-internal: return the raw value. Never bind the result to a long-lived name."""
    return secret._value
