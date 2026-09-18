"""Stage 3: registration.

Registration is import-time, so the checks here are the ones that turn a typo into an
error at import rather than into a mysteriously cold cache months later.
"""

from __future__ import annotations

import pytest

from matview import REGISTRY, Registry, recipe
from matview.errors import CodecError, InvalidKey, MatviewError, RecipeNotFound


def test_signature_defaults_become_the_recipe_defaults(registry: Registry) -> None:
    @recipe(version=2, codec="json", index=("symbol",), registry=registry)
    def bars(ctx, *, symbol: str, interval: str = "1d", window: float = 14.0):
        return {}

    rec = registry.get("bars")
    assert rec.params == ("symbol", "interval", "window")
    assert rec.defaults == {"interval": "1d", "window": 14}, "normalized at registration"
    assert rec.bind({"symbol": "AAPL"}) == {"symbol": "AAPL", "interval": "1d", "window": 14}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_infinite_defaults_are_rejected_at_registration(
    registry: Registry, bad: float
) -> None:
    with pytest.raises(InvalidKey, match="NaN and infinities"):

        @recipe(version=1, codec="json", registry=registry)
        def broken(ctx, *, alpha: float = bad):
            return {}


@pytest.mark.parametrize("reserved", ["on_miss", "force", "force_deps", "deadline", "part"])
def test_parameters_colliding_with_policy_arguments_are_rejected(
    registry: Registry, reserved: str
) -> None:
    """They would be silently swallowed by the policy layer. See DECISIONS.md D18."""
    namespace: dict[str, object] = {}
    exec(
        f"def clash(ctx, *, {reserved}: str = 'x'):\n    return {{}}", namespace
    )
    with pytest.raises(InvalidKey, match="collide with policy arguments"):
        recipe(version=1, codec="json", registry=registry)(namespace["clash"])


def test_an_unknown_codec_is_rejected_at_registration(registry: Registry) -> None:
    with pytest.raises(CodecError, match="unknown codec 'parquet'"):

        @recipe(version=1, codec="parquet", registry=registry)
        def wrong(ctx, *, n: int = 1):
            return {}


def test_an_uninstalled_codec_extra_still_registers(registry: Registry) -> None:
    """A recipe must be registrable on a machine that cannot run it.

    Import-time registration happens everywhere the module is imported, including hosts
    that only read the cache.
    """

    @recipe(version=1, codec="arrow", registry=registry)
    def frames(ctx, *, n: int = 1):
        return {}

    assert registry.get("frames").codec == "arrow"


@pytest.mark.parametrize("version", [0, -1, "3", 1.0, True])
def test_a_version_must_be_a_positive_integer(registry: Registry, version: object) -> None:
    with pytest.raises(MatviewError, match="positive integer"):

        @recipe(version=version, codec="json", registry=registry)  # type: ignore[arg-type]
        def wrong(ctx, *, n: int = 1):
            return {}


def test_an_unknown_on_miss_is_rejected(registry: Registry) -> None:
    with pytest.raises(MatviewError, match="on_miss must be one of"):

        @recipe(version=1, codec="json", on_miss="maybe", registry=registry)  # type: ignore[arg-type]
        def wrong(ctx, *, n: int = 1):
            return {}


def test_an_index_field_must_be_a_parameter(registry: Registry) -> None:
    with pytest.raises(MatviewError, match="are not parameters"):

        @recipe(version=1, codec="json", index=("symbol",), registry=registry)
        def wrong(ctx, *, n: int = 1):
            return {}


def test_var_args_cannot_be_key_fields(registry: Registry) -> None:
    with pytest.raises(MatviewError, match="cannot be canonicalized"):

        @recipe(version=1, codec="json", registry=registry)
        def wrong(ctx, *args, **kwargs):
            return {}


def test_a_recipe_needs_a_context_parameter(registry: Registry) -> None:
    with pytest.raises(MatviewError, match="first parameter is the context"):

        @recipe(version=1, codec="json", registry=registry)
        def wrong():
            return {}


def test_a_version_bump_replaces_the_entry(registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def thing(ctx, *, n: int = 1):
        return {"v": 1}

    @recipe(version=2, codec="json", registry=registry)
    def thing(ctx, *, n: int = 1):  # noqa: F811
        return {"v": 2}

    assert len(registry) == 1
    assert registry.get("thing").version == 2


def test_the_registry_reports_what_it_holds(registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def alpha(ctx, *, n: int = 1):
        return {}

    @recipe(version=1, codec="json", registry=registry)
    def beta(ctx, *, n: int = 1):
        return {}

    assert registry.names() == ["alpha", "beta"]
    assert "alpha" in registry and "gamma" not in registry
    assert {rec.name for rec in registry} == {"alpha", "beta"}

    registry.clear()
    assert len(registry) == 0
    with pytest.raises(RecipeNotFound, match="none registered"):
        registry.get("alpha")


def test_the_decorator_defaults_to_the_global_registry() -> None:
    """The registry is global and import-time by design; `Cache` is what must not be."""
    try:

        @recipe(version=1, codec="json")
        def globally_registered(ctx, *, n: int = 1):
            return {}

        assert "globally_registered" in REGISTRY
        assert REGISTRY.get("globally_registered").fn is globally_registered
    finally:
        REGISTRY.clear()


def test_the_decorator_returns_the_original_function(registry: Registry) -> None:
    @recipe(version=1, codec="json", registry=registry)
    def plain(ctx, *, n: int = 1):
        return {"v": n}

    assert plain(None, n=5) == {"v": 5}, "still an ordinary callable"
