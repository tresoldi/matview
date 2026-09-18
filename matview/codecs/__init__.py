"""Codecs encode a Python object to bytes and back, and optionally split it into parts.

Codecs are named, and the name is recorded per recipe, so decoding is a lookup and
never an inference.

**The byte-canonical requirement.** Because fingerprints key on content digests, a
codec must produce identical bytes for identical input, on every machine, every time.
A codec that fails this defeats early cutoff silently: nothing errors, the cascade
simply never stops.

Concrete rules, from the spec:

- Parquet is disqualified out of the box. It stamps a creation timestamp and writer
  version into the file, so two encodings of the same table differ. Arrow IPC with
  compression off, fixed schema field ordering and no custom metadata is the safe default.
- No codec may embed a timestamp, hostname, PID, or library version.
- Compression, if ever added, must be at a pinned level with a pinned implementation,
  or it silently breaks the guarantee across environments.

Serialization is never by Python object graph, only by an explicit format. That rules
out the obvious shortcut for arbitrary objects, and it is what keeps the cache readable
from other languages and safe to share.
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

from matview.errors import CodecError, CodecUnavailable

__all__ = [
    "CODEC_NAMES",
    "BaseCodec",
    "Codec",
    "CodecError",
    "CodecUnavailable",
    "codec_is_known",
    "get_codec",
    "register_codec",
]


@runtime_checkable
class Codec(Protocol):
    """Encode a value to bytes and back."""

    name: str

    def encode(self, value: object) -> bytes | dict[str, bytes]:
        """Return bytes for one unpartitioned artifact, or a dict of `part -> bytes`."""
        ...

    def decode(self, data: bytes) -> object:
        """Decode one artifact's bytes."""
        ...

    def decode_parts(self, parts: dict[str, bytes]) -> object:
        """Reassemble a partitioned value from every part, in sorted-label order.

        Only partitioning codecs implement this. See DECISIONS.md D11.
        """
        ...


class BaseCodec:
    """Shared behaviour. A codec that never partitions inherits the right refusal."""

    name: str = ""
    partitions: bool = False

    def encode(self, value: object) -> bytes | dict[str, bytes]:  # pragma: no cover - abstract
        raise NotImplementedError

    def decode(self, data: bytes) -> object:  # pragma: no cover - abstract
        raise NotImplementedError

    def decode_parts(self, parts: dict[str, bytes]) -> object:
        raise CodecError(
            f"codec {self.name!r} does not partition, so it cannot reassemble "
            f"{len(parts)} parts. A partitioned recipe needs a partitioning codec."
        )


#: Every codec name this build knows about, whether or not its extra is installed.
CODEC_NAMES: Final = frozenset({"json", "arrow", "arrow-series", "npy"})

_BUILTIN_MODULES: Final = {
    "json": "matview.codecs._json",
    "arrow": "matview.codecs._arrow",
    "arrow-series": "matview.codecs._arrow",
    "npy": "matview.codecs._npy",
}

_REGISTRY: dict[str, Codec] = {}


def register_codec(codec: Codec) -> None:
    """Register a codec under its own name. Re-registering the same name replaces it."""
    if not codec.name:
        raise CodecError("a codec must have a name")
    _REGISTRY[codec.name] = codec


def get_codec(name: str) -> Codec:
    """Look up a codec, importing its optional extra on first use."""
    if name in _REGISTRY:
        return _REGISTRY[name]

    module = _BUILTIN_MODULES.get(name)
    if module is None:
        known = ", ".join(sorted(set(_REGISTRY) | CODEC_NAMES))
        raise CodecError(f"unknown codec {name!r}; known codecs: {known}")

    import importlib

    try:
        importlib.import_module(module)
    except ImportError as exc:
        extra = "arrow" if name.startswith("arrow") else name
        raise CodecUnavailable(
            f"codec {name!r} needs its optional extra: pip install 'matview[{extra}]' ({exc})"
        ) from exc

    if name not in _REGISTRY:  # pragma: no cover - a builtin module that failed to register
        raise CodecError(f"module {module} did not register codec {name!r}")
    return _REGISTRY[name]


def codec_is_known(name: str) -> bool:
    """Whether a codec name resolves, without importing its extra.

    Used at registration so a typo fails at import time rather than at the first
    compute, while an uninstalled extra does not stop a recipe being registered.
    """
    return name in _REGISTRY or name in _BUILTIN_MODULES
