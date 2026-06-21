# Regulation Graph Schema — Design Document (Stage 3, Step 1)

**Project:** Mabhas Compliance Engine — Stage 3 (Graph Extension)
**Status:** Design for committee review. No code in this step.
**Source corpus:** `data/mabhas_clauses_contextual.json` (594 clauses; 328 ingestable).
This file is `mabhas_clauses_normalized.json` plus one additive field, `context_fa`
(see `rag/contextualize.py`); all metadata fields referenced below exist in both.
All counts in this document were measured directly on the corpus snapshot of 2026-06-10.

---

## 1. Purpose

The regulation graph is an explicit, deterministic index of the *structure* of the
Mabhas corpus — which clause governs which building element, constrains which
property, applies to which occupancy, defines which term, and excepts which other
clause — built once from the DeepSeek classification metadata and held in memory as
a NetworkX `MultiDiGraph`. It complements vector retrieval rather than replacing it:
dense/BM25/CRAG retrieval (Stages 1–2) ranks clauses by *textual similarity to the
query*, which systematically misses clauses that are relevant by *reference* rather
than by *wording*. Three failure modes observed in the Stage 1/2 evaluations are
targeted directly. **Multi-hop** (155/477 eval items): the answer requires a second
clause that shares an entity, not vocabulary, with the query — the graph reaches it
in one edge traversal (shared `Element`/`Property` neighbourhood). **Exception
cross-references** (6 exception clauses; headline Stage 3 metric): a retrieved base
rule is silently modified by an exception clause that may share almost no surface
vocabulary with it — the `HAS_EXCEPTION` edge makes this modification retrievable
deterministically, with zero LLM calls. **Definition resolution** (62 definition
clauses): queries and clauses use defined terms (e.g. *فضای اقامت* / dwelling space)
whose normative meaning lives in a glossary clause that embedding similarity ranks
poorly because it states no requirement — `DEFINES`/`USES_TERM` edges attach the
glossary to every clause that uses the term.

## 2. Node Types

All node identifiers are strings with a type prefix, so a single
`nx.MultiDiGraph` can hold the heterogeneous schema without collisions.
Every node carries `node_type` as an attribute.

### 2.1 `Clause`

| | |
|---|---|
| **Represents** | One classified Mabhas article (or split sub-article, e.g. `…a`, `…b`). Only ingestable clauses (`skip_category == null`) become nodes: **328** in the current corpus (166 numeric, 94 spatial, 62 definition, 6 exception). |
| **Source fields** | `article_id`, `mabhas_part`, `rule_type`, `heading_fa`, `text_fa_normalized`, `text_en`, `applicable_occupancies`, `applicable_height_groups`, `context_fa` |
| **Canonical id** | `clause:{article_id}` — e.g. `clause:10-1-1-7-4a`. `article_id` is unique per (`mabhas_part`, `article_id`) by construction of the classification output. |
| **Attributes** | All nine source fields above, stored verbatim. `entities` is *not* stored on the node — it is consumed by the builder to emit edges (§3), keeping the node payload identical in shape across all four rule types. |

### 2.2 `Element`

| | |
|---|---|
| **Represents** | A canonical building-element type — the join key between the RegulationGraph and the `SpatialGraph` (`services/spatial_graph.py`), whose room nodes carry `category` strings (`room_bedroom`, …). |
| **Source fields** | `entities.object` (numeric), `entities.subject` and `entities.object` (spatial) — **after alias normalization** (§4.2). Raw values are free text from DeepSeek (131 distinct numeric objects, 87 distinct spatial subjects measured). |
| **Canonical id** | `element:{canonical_type}` — e.g. `element:kitchen`. |
| **Attributes** | `canonical_type`, `spatial_graph_category` (the `SpatialGraph` category string it maps to, or `null` for elements the floor-plan model does not yet emit). |

**Closed Element vocabulary (21 types).** This list is closed: the builder never
invents a new `Element` node from raw entity text (§4.2).

| `canonical_type` | SpatialGraph mapping | `canonical_type` | SpatialGraph mapping |
|---|---|---|---|
| `bedroom` | `room_bedroom` (node) | `landing` | — (planned) |
| `living_room` | `room_living` (node) | `entrance` | — (planned) |
| `kitchen` | `room_kitchen` (node) | `door` | door **edge** attrs |
| `bathroom` | `room_bathroom` (node) | `window` | room `windows` list |
| `balcony` | `room_balcony` (node) | `courtyard` | — (planned) |
| `storage` | — (planned) | `light_well` | — (planned) |
| `corridor` | — (planned) | `basement` | — (planned) |
| `stair` | — (planned) | `parking` | — (planned) |
| `ramp` | — (planned) | `elevator` | — (planned) |
| `roof` | — (planned) | `dwelling_unit` | — (aggregate) |
| `building` | — (whole graph) | | |

The five `room_*` categories and the door/window mappings are exactly what
`SpatialGraph` and `topology_agent.CATEGORY_SYNONYMS` already emit today; the
"planned" rows are elements the Mask R-CNN/BIM stage does not yet label but which
clauses demonstrably govern (e.g. `courtyard` is the single most frequent numeric
object, 8 clauses). Keeping them in the closed vocabulary now means the graph is
complete on the regulation side and the spatial side can grow into it.

### 2.3 `Occupancy`

| | |
|---|---|
| **Represents** | A Mabhas occupancy group for which a clause creates a requirement. |
| **Source field** | `applicable_occupancies` (list). Codes observed in the ingestable corpus: `all_residential` (316 clauses), `any` (7), `M-1` (2), `M-2` (2), `M-4` (1). `M-3` is defined in the classification scheme but unused; the node set is the five codes plus `M-3` for forward compatibility. |
| **Canonical id** | `occupancy:{code}` — e.g. `occupancy:M-4`. |
| **Attributes** | `code`. Semantics of `all_residential` ⊇ {`M-1`…`M-4`} and `any` ⊇ everything are resolved at **query time** by the graph-aware retriever (Step 4), not materialized as edges — materializing the subsumption would multiply edges without adding information. |

### 2.4 `Property`

| | |
|---|---|
| **Represents** | A measurable quantity constrained by numeric clauses — the vocabulary the `NumericChecker` dispatches on. |
| **Source field** | `entities.property` of numeric clauses, after alias normalization. The raw field contains **53 distinct strings** for what are ~16 underlying quantities (e.g. `width`, `clear width`, `clear_width`, `usable_width`, `minimum width` all denote clear horizontal passage width). |
| **Canonical id** | `property:{name}` — e.g. `property:clear_width`. |
| **Attributes** | `name`. |

**Closed Property vocabulary (16 names)** with the alias normalization derived from
the full measured raw vocabulary:

| Canonical | Raw aliases observed (count) |
|---|---|
| `area` | area (22), floor area (7), minimum area (2), area_per_floor (1) |
| `free_area` | free floor area (1) |
| `area_ratio` | area_ratio (2), ratio_to_floor_area (2), area ratio (1), width_to_length_ratio (1), max_fraction_of_required_lighting_area (1) |
| `width` | width (35), minimum width (3), minimum horizontal dimension (1) |
| `clear_width` | clear_width (4), clear width (2), usable_width (2) |
| `length` | length (5) |
| `depth` | depth (3) |
| `height` | height (22), installation_height (1) |
| `clear_height` | clear height (11), headroom height (1), clearance_height (1), minimum height at lowest point (1), covered_height (1), cover_height (1) |
| `slope` | slope (9), slope_and_drainage (1) |
| `distance` | distance (3), distance_to_adjacent_boundary (2), distance_from_main_entrance (1), distance from property boundary (1), distance from public passage side (1) |
| `count` | count (6), quantity (2), washbasin_count (1), sanitary_service_count (1), max_floors_from_top_served (1) |
| `percentage` | transparent_glass_percentage (1), opening_percentage (1), site_percentage_for_residential_lighting (1), site_percentage_for_kitchen_lighting (1) |
| `lighting_area` | required natural light area (1) |
| `ventilation_area` | minimum ventilation opening area (1) |
| `level_difference` | height_difference_to_main_door_threshold (1), entrance floor level height difference (1)* |
| *(unresolvable, manual review)* | minimum (3), maximum (1), minimum_dimension (1), minimum dimension (1), reduction amount (1), radius (1) |

\* appears in `entities.object` in one clause — see Issue 4.3.
The last row's raw values are genuinely ambiguous DeepSeek outputs; they are routed
to the manual alias table (§4.2) rather than guessed.

### 2.5 `Term`

| | |
|---|---|
| **Represents** | A term whose normative meaning is fixed by a definition clause (e.g. *حیاط خلوت*, *فضای اقامت*). |
| **Source fields** | **Derived**, not read: `entities` is `null` for all 62 definition clauses in the corpus (see Issue 4.0). The term is extracted as: `heading_fa` when present (16/62 clauses), else the leading noun phrase of `text_fa_normalized` before the first definitional delimiter («:», «عبارت است از», «-»), validated by the Step 2 builder's unit tests. |
| **Canonical id** | `term:{term_fa}` with the Persian string in Unicode NFC, ZWNJ-normalized — consistent with the Stage 0 rule that Persian gold values are sourced authoritatively from Persian text. |
| **Attributes** | `term_fa`, `term_en` (best-effort from `text_en`, advisory only). |

## 3. Edge Types

All edges are directed; the multigraph permits parallel edges of different types
between the same pair. Every edge carries `edge_type` and `source` (`"metadata"`,
`"derived"`, or `"manual"`) so the thesis ablation can separate metadata-exact edges
from heuristic ones.

| Edge | Connects | Derivation | Edge attributes |
|---|---|---|---|
| `GOVERNS` | `Clause → Element` | numeric `entities.object`, or spatial `entities.subject`, normalized through the element alias table. One edge per entity dict (10 numeric clauses carry a *list* of entity dicts; each list item is processed independently). Unmapped raw values emit **no** edge and are logged (Issue 4.2). | `raw_text` (the original entity string), `source` |
| `CONSTRAINS_PROPERTY` | `Clause → Property` | numeric `entities.property` through the property alias table (§2.4). | `comparator`, `value`, `unit`, `condition`, `raw_text`, `source` — carrying the threshold on the edge lets a graph query answer "all minimum-width rules for stairs" without re-parsing clause text. |
| `RELATES` | `Clause → Element` | spatial `entities.object`, normalized. `relation` is carried as an attribute, **not** as an edge type: 70 distinct relation strings were measured, of which 5 are handled by `TopologyAgent` and the long tail is singleton free text — a closed edge-type set per relation would be fiction. | `relation`, `raw_text`, `source` |
| `APPLIES_TO_OCCUPANCY` | `Clause → Occupancy` | one edge per code in `applicable_occupancies`. Metadata-exact; no heuristics. | `source="metadata"` |
| `HAS_EXCEPTION` | `Clause → Clause` (base rule → exception clause) | **Derived + manual** (Issue 4.1): a reference parser over the exception clause's `text_fa_normalized`/`text_en` extracts cited article ids, resolved against corpus ids by a three-step matcher (exact → reversed-segment → reversed-section-prefix family), then overridden/completed by a checked-in manual link table `data/exception_links.json`. Direction is base→exception so that expansion at retrieval time is a single `successors()` call from any retrieved base clause. | `match_method` (`exact` \| `reversed` \| `section_family` \| `manual`), `cited_ref` (the id string as printed in the text), `source` |
| `DEFINES` | `Clause → Term` | rule_type `definition`; term derived per §2.5. Exactly one `DEFINES` edge per definition clause. | `source="derived"` |
| `USES_TERM` | `Clause → Term` | heuristic: the NFC/ZWNJ-normalized `term_fa` occurs as a substring of another clause's `text_fa_normalized` (whole-word, both sides delimited by non-letter or string boundary, to avoid e.g. *در* "door/in" false positives on short terms; terms shorter than 3 characters are excluded from `USES_TERM` generation entirely). Self-loops (a definition "using" its own term) are suppressed. | `source="derived"` |

## 4. Unresolved Issues and Policies

### 4.0 Schema-vs-data conflict on `definition`/`exception` entities ⚠

The Stage 3 plan assumed `definition.entities = {term, scope}` and
`exception.entities = {applies_to_article}`. The corpus does not contain this:
**all 62 definition and all 6 exception clauses have `entities: null`**, and the
classification prompt (`classification/AI_PROMPT.txt`, v2.0) *mandates* null
entities for those rule types. Following the project's established rule that the
written specification of the data governs over assumptions about it, the schema
above treats `{term}` and `{applies_to_article}` as **derived** fields the Step 2
builder must materialize (per §2.5 and `HAS_EXCEPTION`), not as fields it reads.
The alternative — re-running DeepSeek classification with a v3 prompt — is recorded
as future work; it would only upgrade `source="derived"` edges to
`source="metadata"` without changing the schema.

### 4.1 Exception clauses with no resolvable `applies_to_article`

**Findings (all 6 exception clauses inspected):** 2 of 6 cite an article id in
text; 4 of 6 cite no article at all (they except a *practice*, e.g. "mechanical
ventilation may replace natural ventilation where…"). Worse, corpus `article_id`s
and in-text citations disagree on segment order — an RTL extraction artifact of the
same family as the Stage 0 momayyez defect: clause `10-1-5-4c` cites section
**4-9-7**, which exists in the corpus only as the reversed-suffix family
`2-7-9-4`, `3-7-9-4`, `4-7-9-4`; the citation **4-4-1-5-1** in clause `2-5-1-4-4`
has *two* segment-permutation candidates (`1-1-4-5-4`, `1-4-1-5-4`) and no exact
reversal match.

**Policy: keep-isolated + deterministic resolver + manual-link table.**
No exception clause is ever dropped — an unlinked exception node still ranks in
vector retrieval exactly as in Stage 2, so the graph can only add recall, never
remove it. The resolver links automatically only when unambiguous (exact match, or
unique reversed/section-family match); every ambiguous or text-free case goes to
`data/exception_links.json`, a hand-curated, git-tracked table. With n = 6, manual
curation costs minutes, is fully auditable by the committee, and avoids putting a
heuristic guess into the headline metric's ground truth.

### 4.2 Numeric/spatial objects that match no canonical Element

**Findings:** 131 distinct raw numeric objects and 87 raw spatial subjects vs. a
21-type closed vocabulary; a long tail is compound free text
("emergency opening in skylight/balcony", "step_or_level_difference_or_wall").

**Policy: alias table, no edge on miss, logged misses.** An `ELEMENT_ALIASES`
mapping (mirroring the proven `CATEGORY_SYNONYMS` pattern in
`services/topology_agent.py`) normalizes keyword-matchable raw values; a raw value
that maps to nothing emits **no** `GOVERNS`/`RELATES` edge and is appended to a
build report (`eval/results/graph_unmapped_entities.json`). Rationale: the clause
node and its text remain fully retrievable through Stages 1–2, so a missing edge
degrades gracefully to the status quo, whereas a wrong edge would inject false
clauses into deterministic-agent context. The build report makes alias-table growth
an explicit, reviewable activity rather than silent guessing.

### 4.3 Defined terms never used by any other clause

**Policy: keep as isolated leaves.** A `Term` node with a `DEFINES` edge but no
incoming `USES_TERM` edges is retained. It costs nothing, keeps the glossary
complete for definition-resolution queries (the user may ask "what is a garden
pit?" even if no requirement clause uses the term), and deleting it would make the
graph's node count depend on a heuristic (`USES_TERM` matching), which would make
ablation numbers unstable across alias-table revisions. Related sub-issue: one
clause carries a property-like string in `entities.object`; such cases follow the
4.2 logged-miss path.

### 4.4 Multi-entity clauses

10 numeric clauses carry a JSON *list* of entity dicts (the classifier split
compound thresholds). Policy: each dict independently emits its own
`GOVERNS`/`CONSTRAINS_PROPERTY` edges; the parallel edges are distinguishable in
the `MultiDiGraph` by their `value`/`comparator` attributes. No clause splitting —
`article_id` remains the retrieval unit, matching the pgvector index.

## 5. Worked Examples (real clauses)

### 5.1 Numeric clause `10-1-1-7-4a`

> *EN:* "In residential occupancies where the independent or open kitchen is used
> only for cooking, it must have a minimum area of 5.50 m²."
> `entities = {object: "kitchen", property: "area", comparator: ">=", value: 5.5,
> unit: "m2", condition: "when the kitchen is independent or open and used only
> for cooking"}`; `applicable_occupancies = ["all_residential"]`.

Nodes created (or reused if already present):

1. `clause:10-1-1-7-4a` — Clause node with the nine §2.1 attributes.
2. `element:kitchen` — Element (`spatial_graph_category="room_kitchen"`).
3. `property:area` — Property.
4. `occupancy:all_residential` — Occupancy.

Edges created:

1. `clause:10-1-1-7-4a ─GOVERNS→ element:kitchen` (`raw_text="kitchen"`, `source="metadata"`)
2. `clause:10-1-1-7-4a ─CONSTRAINS_PROPERTY→ property:area` (`comparator=">="`, `value=5.5`, `unit="m2"`, `condition="when the kitchen is independent or open…"`, `source="metadata"`)
3. `clause:10-1-1-7-4a ─APPLIES_TO_OCCUPANCY→ occupancy:all_residential` (`source="metadata"`)

A floor-plan kitchen node (`category="room_kitchen"`) now reaches every kitchen
clause in two hops: SpatialGraph room → `element:kitchen` → incoming `GOVERNS`.

### 5.2 Exception clause `10-1-5-4c`

> *FA (normalized):* «در صورت عدم امکان نورگیری راه‌پله‌ها با پنجره‌های دیواری،
> تأمین نور طبیعی از سقف محفظه پلکان نیز منطبق با الزامات قسمت 4-9-7 مجاز است.»
> *EN:* "If natural lighting of stairways through wall windows is not possible,
> providing natural light from the ceiling of the stairwell is permitted in
> accordance with the requirements of section 4-9-7." `entities = null`.

Nodes: 1. `clause:10-1-5-4c` (reusing `occupancy:all_residential`). The clause also
yields a derived `GOVERNS` edge candidate via the alias table («راه‌پله» /
"stairway" → `element:stair`).

Edges:

1. `clause:10-1-5-4c ─APPLIES_TO_OCCUPANCY→ occupancy:all_residential` (`source="metadata"`)
2. `clause:10-1-5-4c ─GOVERNS→ element:stair` (`raw_text="stairways"`, `source="derived"`)
3. Reference resolution: the parser extracts the citation `4-9-7`; exact match
   fails; full reversal `7-9-4` fails; reversed-section-prefix matching finds the
   family `{2-7-9-4, 3-7-9-4, 4-7-9-4}` (corpus ids for clauses 4-9-7-2/-3/-4).
   Three edges are emitted:
   `clause:2-7-9-4 ─HAS_EXCEPTION→ clause:10-1-5-4c`,
   `clause:3-7-9-4 ─HAS_EXCEPTION→ clause:10-1-5-4c`,
   `clause:4-7-9-4 ─HAS_EXCEPTION→ clause:10-1-5-4c`
   (each with `match_method="section_family"`, `cited_ref="4-9-7"`,
   `source="derived"`), subject to confirmation in `data/exception_links.json`.

Retrieval payoff: a query about stairwell natural lighting that retrieves any
4-9-7-* base clause expands deterministically — one `successors()` call, zero LLM
calls — to the skylight exception, which shares almost no vocabulary with the base
rule and which Stage 1/2 retrieval must find on similarity alone.

---

## Thesis framing (schema choice)

We model the Mabhas corpus as a typed property graph rather than instantiating a
full formal ontology such as AEC3PO, the building-compliance ontology developed in
the ACCORD project, whose expressiveness (OWL axioms, document provenance,
deontic qualifiers) exceeds what a retrieval-augmentation layer can exploit and
whose population would itself become a thesis-sized annotation effort. Our schema
can instead be read as a minimal working instantiation of the same modelling
commitments — regulatory statements as first-class nodes, typed links to the
building elements and quantities they constrain, and explicit exception
relations — that Jiang, Shi and Wang (2022) identify as the core of multi-ontology
fusion for BIM-based automated rule checking, where regulation knowledge and
building-model knowledge live in separate ontologies joined by element-type
alignment. The property-graph reduction is deliberate: every node and edge is
derivable deterministically from already-validated classification metadata (with
derivation provenance stored per edge), the closed Element vocabulary doubles as
the alignment interface to the geometric SpatialGraph, and the whole structure
remains a NetworkX object that the deterministic agents can traverse without any
reasoner in the verdict path — preserving the system's "deterministic spine, AI on
the wings" invariant while still capturing the cross-reference structure that flat
retrieval provably misses.
