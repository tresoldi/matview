"""Recipe registration.

Registration is import-time and global. The registry maps `name` to the function, its
declared version, codec, index fields and policy ceiling. Parameter defaults are read
off the signature and applied before key hashing.

The registry being global will tempt an implementer into making `Cache` global too. It
must not be, which is why every `Cache` takes its registry explicitly and merely
defaults to this one.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from matview.canon import RESERVED_KEY_FIELDS, Scalar, canonicalize_key, normalize_scalar
from matview.codecs import CODEC_NAMES, codec_is_known
from matview.errors import CodecError, InvalidKey, MatviewError, RecipeNotFound
from matview.types import OnMiss

__all__ = ["REGISTRY", "ForeignSpec", "Recipe", "Registry", "recipe"]

_VALID_ON_MISS: Final = ("compute", "enqueue", "raise")


@dataclass(frozen=True, slots=True)
class ForeignInput:
    """One declared input of a foreign recipe, mapped from that recipe's own key."""

    name: str
    recipe: str
    key: Mapping[str, Any]
    part: str | None = None


@dataclass(frozen=True, slots=True)
class ForeignSpec:
    """Everything needed to invoke a recipe written in another language.

    Unlike Python recipes, foreign recipes declare their dependencies upfront. This is
    the privileged case, not a degraded one: a declared graph can be cycle-checked and
    planned before anything executes, whereas a Python recipe's graph is only knowable
    by running it.
    """

    command: tuple[str, ...]
    inputs: tuple[ForeignInput, ...]
    source: str | None
    manifest_path: str | None
    param_types: Mapping[str, str] = field(default_factory=dict)
    workdir: str | None = None


@dataclass(frozen=True, slots=True)
class Recipe:
    """A named, versioned computation. A *kind* of computation, not a particular result."""

    name: str
    version: int
    codec: str
    index: tuple[str, ...]
    on_miss: OnMiss
    deterministic: bool
    max_age: int | None
    params: tuple[str, ...]
    defaults: Mapping[str, Scalar]
    fn: Callable[..., Any] | None = None
    source_hash: str | None = None
    foreign: ForeignSpec | None = None

    def bind(self, params: Mapping[str, Any]) -> dict[str, Scalar]:
        """Fill declared defaults, then canonicalize.

        Defaults are applied **before** hashing, so `sma(symbol="AAPL", window=200)` and
        `sma(symbol="AAPL", window=200, interval="1d")` are the same artifact when
        `interval` defaults to `"1d"`.
        """
        unknown = sorted(set(params) - set(self.params))
        if unknown:
            raise InvalidKey(
                f"{self.name} has no parameter(s) {', '.join(unknown)}; "
                f"it takes {', '.join(self.params) or '(nothing)'}"
            )
        merged: dict[str, Any] = dict(self.defaults)
        merged.update(params)
        missing = sorted(set(self.params) - set(merged))
        if missing:
            raise InvalidKey(f"{self.name} is missing required parameter(s) {', '.join(missing)}")
        return canonicalize_key(merged)

    def index_fields(self, key: Mapping[str, Scalar]) -> dict[str, str]:
        """The subset of the key promoted to real columns for queryability."""
        from matview.canon import index_value

        return {f: index_value(key[f]) for f in self.index if key.get(f) is not None}


class Registry:
    """A name-to-recipe mapping. A version bump replaces the entry."""

    def __init__(self) -> None:
        self._recipes: dict[str, Recipe] = {}

    def register(self, recipe: Recipe) -> Recipe:
        self._recipes[recipe.name] = recipe
        return recipe

    def get(self, name: str) -> Recipe:
        try:
            return self._recipes[name]
        except KeyError:
            known = ", ".join(sorted(self._recipes)) or "(none registered)"
            raise RecipeNotFound(f"no recipe named {name!r}; registered: {known}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._recipes

    def __iter__(self) -> Iterator[Recipe]:
        return iter(self._recipes.values())

    def __len__(self) -> int:
        return len(self._recipes)

    def names(self) -> list[str]:
        return sorted(self._recipes)

    def clear(self) -> None:
        self._recipes.clear()


#: The global registry the decorator writes to by default.
REGISTRY: Final = Registry()


def _validate(
    *,
    name: str,
    version: int,
    codec: str,
    index: tuple[str, ...],
    on_miss: str,
    max_age: int | None,
    params: tuple[str, ...],
    defaults: Mapping[str, Any],
) -> dict[str, Scalar]:
    if not name:
        raise MatviewError("a recipe needs a name")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise MatviewError(f"{name}: version must be a positive integer, got {version!r}")
    if on_miss not in _VALID_ON_MISS:
        raise MatviewError(
            f"{name}: on_miss must be one of {', '.join(_VALID_ON_MISS)}, got {on_miss!r}"
        )
    if max_age is not None and (
        not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 1
    ):
        raise MatviewError(f"{name}: max_age must be a positive integer number of seconds")
    if not codec_is_known(codec):
        known = ", ".join(sorted(CODEC_NAMES))
        raise CodecError(f"{name}: unknown codec {codec!r}; known codecs: {known}")

    reserved = sorted(set(params) & RESERVED_KEY_FIELDS)
    if reserved:
        raise InvalidKey(
            f"{name}: parameter(s) {', '.join(reserved)} collide with policy arguments to "
            f"get(); rename them. Reserved: {', '.join(sorted(RESERVED_KEY_FIELDS))}"
        )

    unknown_index = sorted(set(index) - set(params))
    if unknown_index:
        raise MatviewError(
            f"{name}: index field(s) {', '.join(unknown_index)} are not parameters of the recipe"
        )

    # NaN and infinities are rejected at registration as well as at call time.
    return {field: normalize_scalar(field, value) for field, value in defaults.items()}


def recipe(
    *,
    version: int,
    codec: str,
    index: tuple[str, ...] | list[str] = (),
    on_miss: OnMiss = "compute",
    deterministic: bool = True,
    max_age: int | None = None,
    registry: Registry | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a function as a recipe.

    `deterministic=False` declares that the output varies run to run with identical
    inputs (sampling, unseeded RNG, wall-clock, a network fetch). Such a recipe defeats
    early cutoff and cascades unconditionally. The flag does not change scheduling; it
    exists so the cost is visible and `plan()` can report it, and nobody spends an
    afternoon wondering why one branch always rebuilds. See DECISIONS.md D4.

    `max_age` is a generic freshness ceiling in seconds. Matview decides whether a
    stored value is old; callers still decide when to read it. See DECISIONS.md D34.
    """
    target = REGISTRY if registry is None else registry
    index_fields = tuple(index)

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(fn)
        parameters = list(signature.parameters.values())
        if not parameters:
            raise MatviewError(f"{fn.__name__}: a recipe's first parameter is the context")
        for extra in parameters[1:]:
            if extra.kind in (extra.VAR_POSITIONAL, extra.VAR_KEYWORD):
                raise MatviewError(
                    f"{fn.__name__}: *args and **kwargs are not key fields and cannot be "
                    f"canonicalized; declare parameters explicitly"
                )

        params = tuple(p.name for p in parameters[1:])
        defaults = {
            p.name: p.default for p in parameters[1:] if p.default is not inspect.Parameter.empty
        }
        normalized = _validate(
            name=fn.__name__,
            version=version,
            codec=codec,
            index=index_fields,
            on_miss=on_miss,
            max_age=max_age,
            params=params,
            defaults=defaults,
        )

        target.register(
            Recipe(
                name=fn.__name__,
                version=version,
                codec=codec,
                index=index_fields,
                on_miss=on_miss,
                deterministic=deterministic,
                max_age=max_age,
                params=params,
                defaults=normalized,
                fn=fn,
            )
        )
        return fn

    return decorate


def register_foreign(
    registry: Registry,
    *,
    name: str,
    version: int,
    codec: str,
    index: tuple[str, ...],
    on_miss: OnMiss,
    deterministic: bool,
    max_age: int | None,
    params: tuple[str, ...],
    defaults: Mapping[str, Any],
    spec: ForeignSpec,
    source_hash: str | None,
) -> Recipe:
    """Register a foreign recipe exactly as if a decorator had."""
    normalized = _validate(
        name=name,
        version=version,
        codec=codec,
        index=index,
        on_miss=on_miss,
        max_age=max_age,
        params=params,
        defaults=defaults,
    )
    return registry.register(
        Recipe(
            name=name,
            version=version,
            codec=codec,
            index=index,
            on_miss=on_miss,
            deterministic=deterministic,
            max_age=max_age,
            params=params,
            defaults=normalized,
            fn=None,
            source_hash=source_hash,
            foreign=spec,
        )
    )
