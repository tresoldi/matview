# API reference

Generated from the source.

## Cache

::: matview.cache.Cache

## Context

::: matview.context.Context

## Registration

::: matview.registry.recipe

::: matview.registry.Recipe

::: matview.registry.Registry

## Policy and reports

::: matview.policy.Policy

::: matview.types.PlanReport

::: matview.types.MissingNode

::: matview.types.OnMiss

::: matview.types.stricter

## Artifacts and edges

::: matview.types.Artifact

::: matview.types.ArtifactRef

::: matview.types.DepEdge

::: matview.types.StoreResult

## Graph queries

Lineage, impact and cost over the DAG the cache recorded as it ran. See the
[Graph queries](graph.md) page for what each one answers and how to read it.

::: matview.graph.lineage

::: matview.graph.impact_of_change

::: matview.graph.impact_of_bump

::: matview.graph.impact_of_drop

::: matview.graph.embodied_cost

::: matview.graph.critical_path

::: matview.graph.recipe_costs

::: matview.graph.ArtifactGraph

::: matview.graph.ImpactReport

::: matview.graph.ImpactGroup

::: matview.graph.CostReport

::: matview.graph.GraphStore

## Storage

::: matview.store.base.Store

::: matview.store.sqlite.SqliteStore

## Codecs

::: matview.codecs.Codec

::: matview.codecs.BaseCodec

::: matview.codecs.get_codec

::: matview.codecs.register_codec

## Foreign recipes

::: matview.foreign.load_manifests

::: matview.foreign.load_manifest

::: matview.foreign.check_declared_cycles

## Canonicalization and fingerprints

The interoperability contract these implement is `SPEC.md`, versioned independently of
the code.

::: matview.fingerprint

::: matview.canon.canonicalize_key

::: matview.canon.jcs

## Errors

::: matview.errors
