# Configuration surface architecture

This note explains how the generator models Codex configuration as part of the
same contract graph as requests, metadata, tools, routes, authentication, and
context management. It deliberately does **not** enumerate current settings or
copy their descriptions. Those details change faster than architecture prose;
the generated machine-readable JSON is the authority.

## Why configuration is a protocol surface

A configuration key is not only an input accepted by a parser. It may select a
provider, alter authentication, enable a model-visible tool, change request
metadata, redirect an endpoint plane, or activate a context lifecycle. A useful
contract therefore needs to answer both questions:

```text
What values and shapes can be configured?
              +
What observable surface can each configuration path affect?
```

A schema alone answers the first question. The surface graph joins it to the
second.

## Evidence planes

The implementation keeps three evidence planes separate and then connects them.

### 1. Generated shape

`codex-rs/core/config.schema.json` is ingested as exact source bytes. Local
references are resolved and normalized into a path-addressable catalog. Each
catalog entry retains type, requiredness, constraints, composition, source
locations, and a semantic digest.

The catalog includes root configuration, profile-scoped configuration, arrays,
and dynamic maps. It is an inventory of accepted shape, not a claim about
runtime behavior.

### 2. Registry and schema-projection policy

The Rust `FEATURES` constant defines feature identity, public key, lifecycle
stage, and default state. The generator parses only that constant; unrelated
uses of the `FeatureSpec` type are not treated as registry entries.

`features_schema()` is a second source of truth. It describes how registry
entries become user-facing schema properties. This matters because the mapping
is not always one feature to one top-level property:

```text
feature registry entry
       │
       ├── direct boolean property
       ├── structured feature table
       ├── embedded field in another structured feature table
       └── intentionally omitted from user configuration
```

The crosswalk records the representation explicitly. A registry key absent from
the generated schema is therefore an error unless the schema generator proves
that omission. A dotted registry key can be accounted for by an exact nested
schema path rather than being misclassified as missing.

### 3. Runtime effects

Canonical extractors and reviewed effect specifications connect configuration
paths to existing protocol entities. Examples of target categories include
provider transport, request bodies, headers, metadata, tool exposure, endpoint
routing, and context-window behavior.

Where an older config-effect fact still comes from the frozen compatibility
catalog, its edge is labeled `legacy_compatibility`. It remains navigable but
cannot silently satisfy a canonical proof requirement.

## The composed graph

The graph is intentionally typed rather than represented as a flat settings
list.

```text
config path
    │ configures_feature
    ▼
feature identity
    │ has_schema_projection_policy
    ▼
schema projection policy
    │ materializes_as_config_path
    └──────────────────────────────┐
                                   │
config path ── enables/routes/selects/projects_to ──> protocol surface
```

Primary node kinds are:

- `config_path`: a normalized path from the generated schema;
- `feature`: a registry-owned feature identity;
- `config_schema_policy`: the rule by which that feature is exposed or omitted;
- protocol surface nodes: routes, headers, request fields, metadata, tools,
  authentication, context lifecycle, and other canonical entities;
- `legacy_compatibility_surface`: retained historical evidence that has not yet
  migrated to a native extractor.

Bidirectional views allow consumers to start from a config path, feature, or
surface. The graph does not imply that every accepted setting has a proven
runtime effect. Missing behavioral edges remain visible as coverage gaps.

## Full-picture composition

Configuration joins the prior work rather than replacing it:

```text
Generated config schema
        +
Feature registry and schema policy
        +
Existing canonical extractors
        +
Versioned historical contracts
        +
Runtime evidence profiles
        =
One navigable, provenance-bound surface graph
```

This makes distinctions that are easy to lose in prose:

- accepted configuration versus internal-only capability;
- activation eligibility versus transport routing;
- top-level settings versus profile-scoped settings;
- model-provider traffic versus ChatGPT auxiliary traffic;
- current canonical behavior versus retained legacy evidence;
- static source proof versus observed runtime conformance.

## Machine-readable authority

Human documentation explains ownership and architecture. Consumers should use
the generated artifacts for current names, types, defaults, enums, constraints,
and exact links:

- `config-schema.json` — normalized path catalog derived from the generated
  Codex schema;
- `config-surface-graph.json` — typed nodes and edges joining configuration to
  previously modeled surfaces;
- the `extractor.config_effects` section in the main report — registry,
  projection policy, crosswalk, effect links, diagnostics, and coverage;
- integration and proof records — exact source revision, hashes, status, and
  failure diagnostics.

This separation is intentional. A documentation update may lag a Codex change;
a successful pinned generation must not.

## Drift and failure semantics

The configuration proof fails visibly when, among other things:

- the generated schema is invalid or contains unresolved local references;
- the `FEATURES` registry cannot be bounded or parsed losslessly;
- a schema-generator branch cannot be classified;
- a feature is neither materialized nor explicitly accounted for;
- a required full-picture anchor disappears;
- an effect specification points to a missing config path;
- graph edges reference unknown nodes;
- pinned source identity or generated artifact validation fails.

A current-main canary may report drift, but it does not replace the reviewed
pinned baseline. Updating the baseline is a review decision, not an automatic
side effect of generation.

## Ownership boundaries

The generated schema owns accepted shape. The feature registry owns feature
identity and lifecycle. The schema generator owns exposure policy. Canonical
extractors own runtime semantics. The graph composer owns relationships, not
the underlying facts. CI owns reproducibility and fail-visible drift.

Keeping those boundaries explicit avoids turning one parser or document into an
unreviewable source of truth for the entire product surface.
