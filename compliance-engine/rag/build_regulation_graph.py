"""
RAG/build_regulation_graph.py
=============================
Stage 3, Step 2 — Regulation Graph Builder.

Builds the typed property graph specified in RAG/regulation_graph_schema.md
(Stage 3, Step 1) from the classified Mabhas corpus and writes it to disk as
GraphML, together with a plain-text coverage report.

Usage
-----
    python -m rag.build_regulation_graph \
        --input  data/mabhas_clauses_contextual.json \
        --output data/regulation_graph.graphml \
        --report docs/regulation_graph_report.txt \
        [--manual-links data/exception_links.json]

Design notes (see the schema document for the full rationale)
-------------------------------------------------------------
* Graph type: networkx.MultiDiGraph — multiple typed, directed edges may
  connect the same node pair (e.g. GOVERNS and CONSTRAINS_PROPERTY targets,
  or several CONSTRAINS_PROPERTY edges from one multi-entity clause).
* Serialization: GraphML. GraphML only supports scalar attribute values, so
  lists and None are JSON-encoded behind a "__json__:" sentinel on write and
  decoded by load_regulation_graph(). Round-trip equality is asserted in
  eval/test_regulation_graph.py. (Pickle would round-trip natively but is
  neither portable nor human-inspectable; GraphML keeps the thesis artifact
  open to Gephi/yEd inspection by the committee.)
* Provenance: every edge carries source = "metadata" | "derived" | "manual"
  so ablations can separate metadata-exact structure from heuristics.
* Deterministic only: no LLM calls anywhere in this module.

Public surface
--------------
    build_graph(clauses, manual_links=None)  -> (nx.MultiDiGraph, BuildStats)
    save_regulation_graph(G, path)           -> None      (GraphML, encoded)
    load_regulation_graph(path)              -> nx.MultiDiGraph (decoded)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import networkx as nx

# ═══════════════════════════════════════════════════════════════════════════
# Closed vocabularies (schema §2.2, §2.4)
# ═══════════════════════════════════════════════════════════════════════════

# canonical_type -> SpatialGraph category string (None = not yet emitted by
# the floor-plan model; "planned" rows in the schema document).
CANONICAL_ELEMENTS: Dict[str, Optional[str]] = {
    "bedroom":       "room_bedroom",
    "living_room":   "room_living",
    "kitchen":       "room_kitchen",
    "bathroom":      "room_bathroom",
    "balcony":       "room_balcony",
    "storage":       None,
    "corridor":      None,
    "stair":         None,
    "ramp":          None,
    "landing":       None,
    "entrance":      None,
    "door":          None,   # SpatialGraph door *edge* attributes
    "window":        None,   # SpatialGraph room "windows" attribute list
    "courtyard":     None,
    "light_well":    None,
    "basement":      None,
    "parking":       None,
    "elevator":      None,
    "roof":          None,
    "dwelling_unit": None,
    "building":      None,
}

# Ordered keyword table: first matching keyword wins. Keys are substrings
# matched against the normalized raw entity string (lowercased, '_'/'-' to
# spaces). Order matters: specific before generic ("light well" before
# "courtyard", "unit" before "dwelling", glazing before its host space).
# Mirrors the CATEGORY_SYNONYMS pattern of services/topology_agent.py.
ELEMENT_KEYWORDS: List[Tuple[str, str]] = [
    ("light well", "light_well"),
    ("lightwell", "light_well"),
    ("light and air", "light_well"),   # مجرای نور و هوا (external light/air duct)
    ("courtyard", "courtyard"),
    ("garden pit", "courtyard"),
    ("patio", "courtyard"),
    ("skylight", "window"),
    ("glaz", "window"),
    ("glass", "window"),
    ("window", "window"),
    ("emergency opening", "window"),
    ("opening", "window"),
    ("door", "door"),
    ("lock", "door"),                  # chain_or_safety_lock (main-door hardware)
    ("chain", "door"),
    ("entrance", "entrance"),
    ("entry", "entrance"),
    ("lobby", "entrance"),
    ("vestibule", "entrance"),
    ("stair", "stair"),
    ("step", "stair"),
    ("handrail", "stair"),
    ("ramp", "ramp"),
    ("landing", "landing"),
    ("turning", "parking"),            # vehicle turning path/space (section 4-5-10)
    ("corridor", "corridor"),
    ("hallway", "corridor"),
    ("passage", "corridor"),
    ("circulation", "corridor"),
    ("egress", "corridor"),
    ("escape route", "corridor"),
    ("route", "corridor"),
    ("exit", "door"),
    ("kitchen", "kitchen"),
    ("sanitary", "bathroom"),
    ("bathroom", "bathroom"),
    ("toilet", "bathroom"),
    ("washbasin", "bathroom"),
    ("shower", "bathroom"),
    ("wc", "bathroom"),
    ("bedroom", "bedroom"),
    ("dormitory", "bedroom"),
    ("accommodation", "bedroom"),      # accommodation_space (فضای اقامت)
    ("living", "living_room"),
    ("salon", "living_room"),
    ("multipurpose", "living_room"),
    ("multi purpose", "living_room"),
    (" play", "living_room"),          # leading-space boundary avoids "display"
    ("childcare room", "living_room"),
    ("semi open", "balcony"),          # فضاهای نیمه باز (before generic "open space")
    ("balcony", "balcony"),
    ("terrace", "balcony"),
    ("iwan", "balcony"),
    ("veranda", "balcony"),
    ("sunroom", "balcony"),
    ("sunshade", "balcony"),           # محفظه آفتاب‌گیر (sunshade enclosure)
    ("refuge", "balcony"),             # safety refuge on semi-open spaces (4-5-7)
    ("open space", "courtyard"),
    ("storage", "storage"),
    ("storeroom", "storage"),
    ("store room", "storage"),
    ("basement", "basement"),
    ("parking", "parking"),
    ("garage", "parking"),
    ("elevator", "elevator"),
    ("lift", "elevator"),
    ("roof", "roof"),
    ("ceiling", "roof"),
    ("eave", "roof"),
    ("unit", "dwelling_unit"),
    ("apartment", "dwelling_unit"),
    ("dwelling", "bedroom"),       # numeric_checker maps dwelling_space -> room_bedroom
    ("habitable", "bedroom"),
    ("occupied space", "bedroom"),
    ("occupancy space", "bedroom"),
    ("room", "bedroom"),           # generic "room"/"basement_room" fallback, last room-ish rule
    ("building", "building"),
    ("facade", "building"),
    ("protrusion", "building"),
    ("setback", "building"),
    ("wall", "building"),
    ("floor", "building"),
]

# Raw entities.property -> canonical Property name (schema §2.4). Keys are
# normalized (lowercase, '_'/'-' to single spaces). Raw values absent from
# this table emit no CONSTRAINS_PROPERTY edge and are logged.
PROPERTY_ALIASES: Dict[str, str] = {
    "area": "area",
    "floor area": "area",
    "minimum area": "area",
    "area per floor": "area",
    "free floor area": "free_area",
    "area ratio": "area_ratio",
    "ratio to floor area": "area_ratio",
    "width to length ratio": "area_ratio",
    "max fraction of required lighting area": "area_ratio",
    "width": "width",
    "minimum width": "width",
    "minimum horizontal dimension": "width",
    "clear width": "clear_width",
    "usable width": "clear_width",
    "length": "length",
    "depth": "depth",
    "height": "height",
    "installation height": "height",
    "clear height": "clear_height",
    "headroom height": "clear_height",
    "clearance height": "clear_height",
    "minimum height at lowest point": "clear_height",
    "minimum height": "clear_height",
    "covered height": "clear_height",
    "cover height": "clear_height",
    "slope": "slope",
    "slope and drainage": "slope",
    "distance": "distance",
    "distance to adjacent boundary": "distance",
    "distance from main entrance": "distance",
    "distance from property boundary": "distance",
    "distance from public passage side": "distance",
    "count": "count",
    "quantity": "count",
    "washbasin count": "count",
    "sanitary service count": "count",
    "max floors from top served": "count",
    "transparent glass percentage": "percentage",
    "opening percentage": "percentage",
    "site percentage for residential lighting": "percentage",
    "site percentage for kitchen lighting": "percentage",
    "required natural light area": "lighting_area",
    "minimum ventilation opening area": "ventilation_area",
    "height difference to main door threshold": "level_difference",
    "entrance floor level height difference": "level_difference",
}

# Heading-derived Term candidates that name a *section*, not a term.
TERM_STOPLIST = {"تعاريف", "تعاریف", "کلیات", "كليات"}

# Definitional delimiters for leading-noun-phrase term extraction (§2.5).
_TERM_DELIM_RE = re.compile(r"[:：]|عبارت است از")
# Leading list markers: dashes/digits and single-letter Persian list labels
# such as "-4 ", "پ- 1- ", "ت- 2- ".
_LIST_MARKER_RE = re.compile(
    r"^(?:[\s\-–ـ]+|[0-9۰-۹]+[\s\-–ـ]+|[\u0600-\u06FF][\s\-–ـ]+(?=[\s0-9۰-۹\-–]))+"
)
_PERSIAN_LETTER_RE = re.compile(r"[\u0621-\u064A\u0660-\u0669\u067E\u0686\u0698\u06A9\u06AF\u06CC]")

# Article references in exception text: at least three dash-joined integers
# (two-segment runs collide with table/group references like "جدول 4-6").
_ARTICLE_REF_RE = re.compile(r"\b\d+(?:-\d+){2,}[a-z]?\b")

EDGE_TYPES = (
    "GOVERNS", "CONSTRAINS_PROPERTY", "RELATES", "APPLIES_TO_OCCUPANCY",
    "HAS_EXCEPTION", "DEFINES", "USES_TERM",
)

CLAUSE_ATTRS = (
    "article_id", "mabhas_part", "rule_type", "heading_fa",
    "text_fa_normalized", "text_en", "applicable_occupancies",
    "applicable_height_groups", "context_fa",
)


# ═══════════════════════════════════════════════════════════════════════════
# Stats container
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class BuildStats:
    """Everything the coverage report needs, accumulated during the build."""
    node_counts: Counter = field(default_factory=Counter)
    edge_counts: Counter = field(default_factory=Counter)
    rule_type_counts: Counter = field(default_factory=Counter)
    numeric_total: int = 0
    numeric_linked: int = 0                 # numeric clauses with >=1 GOVERNS
    exception_total: int = 0
    exception_resolved: int = 0             # exception clauses with >=1 incoming HAS_EXCEPTION
    unmapped_elements: List[Dict[str, str]] = field(default_factory=list)
    unmapped_properties: List[Dict[str, str]] = field(default_factory=list)
    unresolved_exceptions: List[Dict[str, Any]] = field(default_factory=list)
    definitions_without_term: List[str] = field(default_factory=list)
    observed_elements: Counter = field(default_factory=Counter)
    exception_links: List[Dict[str, str]] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════════════
# Normalization / mapping helpers
# ═══════════════════════════════════════════════════════════════════════════

def _norm_raw(value: Any) -> str:
    """Normalize a raw entity string for alias matching."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    s = str(value).lower().replace("_", " ").replace("-", " ").replace("‑", " ")
    return re.sub(r"\s+", " ", s).strip()


def map_element(raw: Any) -> Optional[str]:
    """Raw entities.object/subject -> canonical Element type, or None."""
    s = _norm_raw(raw)
    if not s:
        return None
    padded = f" {s} "          # lets keywords anchor on word boundaries
    for keyword, canonical in ELEMENT_KEYWORDS:
        if keyword in padded:
            return canonical
    return None


def map_property(raw: Any) -> Optional[str]:
    """Raw entities.property -> canonical Property name, or None."""
    return PROPERTY_ALIASES.get(_norm_raw(raw))


def extract_term(clause: Dict[str, Any]) -> Optional[str]:
    """
    Derive the defined term of a definition clause (schema §2.5).

    Precedence: the glossary colon-pattern in the text wins over heading_fa,
    because headings frequently name the *section* («تعاريف») rather than
    the term; the colon pattern («پاسیو: فضایی باز است …») is the term by
    construction.
    """
    text = clause.get("text_fa_normalized") or ""
    m = _TERM_DELIM_RE.search(text[:80])
    if m and m.start() > 0:
        candidate = _LIST_MARKER_RE.sub("", text[: m.start()]).strip(" \t-–ـ")
        if _is_valid_term(candidate):
            return candidate
    heading = (clause.get("heading_fa") or "").strip()
    if heading and heading not in TERM_STOPLIST and _is_valid_term(heading):
        return heading
    return None


def _is_valid_term(candidate: str) -> bool:
    if not candidate or len(candidate) < 3 or len(candidate) > 60:
        return False
    if candidate in TERM_STOPLIST:
        return False
    if not _PERSIAN_LETTER_RE.search(candidate):
        return False
    return len(candidate.split()) <= 6


def _entity_dicts(clause: Dict[str, Any]) -> List[Dict[str, Any]]:
    """entities may be null, a dict, or a list of dicts (10 numeric clauses)."""
    e = clause.get("entities")
    if e is None:
        return []
    if isinstance(e, dict):
        return [e]
    return [d for d in e if isinstance(d, dict)]


# ═══════════════════════════════════════════════════════════════════════════
# Exception reference resolution (schema §3 HAS_EXCEPTION, §4.1)
# ═══════════════════════════════════════════════════════════════════════════

def parse_article_refs(clause: Dict[str, Any]) -> List[str]:
    """
    Extract cited article ids (>=3 segments) from the clause text.

    A match at the very start of a text is the clause's own printed label
    (e.g. «3-10-1-5-4 در صورت …» in clause 10-1-5-4c) and is skipped — it is
    a self-reference, not a citation.
    """
    refs, seen = [], set()
    for raw in (clause.get("text_fa_normalized"), clause.get("text_en")):
        text = (raw or "").replace("‑", "-").lstrip()  # NB-hyphen in text_en
        for m in _ARTICLE_REF_RE.finditer(text):
            if m.start() <= 3:              # printed label position
                continue
            ref = m.group(0)
            if ref not in seen and ref != clause.get("article_id"):
                seen.add(ref)
                refs.append(ref)
    return refs


def _reverse_segments(ref: str) -> str:
    m = re.match(r"^(.*?)([a-z])?$", ref)
    core, suffix = m.group(1), m.group(2) or ""
    return "-".join(reversed(core.split("-"))) + suffix


def resolve_reference(ref: str, clause_ids: Iterable[str],
                      self_id: str) -> Tuple[List[str], str]:
    """
    Resolve one cited article id against corpus clause ids.

    Returns (matched_ids, match_method). Methods, tried in order:
      exact           ref is itself a corpus id
      reversed        full segment reversal (RTL extraction artifact)
      section_family  reversed ref is the *suffix* of corpus ids (a cited
                      section matching its sub-clauses), unique-prefix safe
      parent_section  drop the ref's last segment once, retry exact/reversed
                      (a cited sub-clause whose parent was stored un-split)
    Empty list + "unresolved" when nothing matches unambiguously.
    """
    ids = [i for i in clause_ids if i != self_id]
    if ref in ids:
        return [ref], "exact"
    rev = _reverse_segments(ref)
    if rev in ids:
        return [rev], "reversed"
    family = sorted(i for i in ids if i.endswith("-" + rev) or
                    re.match(rf"^{re.escape(rev)}[a-z]$", i))
    if family:
        return family, "section_family"
    parent = "-".join(ref.split("-")[:-1])
    if parent.count("-") >= 1:  # keep at least two segments
        if parent in ids:
            return [parent], "parent_section"
        rev_parent = _reverse_segments(parent)
        if rev_parent in ids:
            return [rev_parent], "parent_section"
    return [], "unresolved"


def load_manual_links(path: Optional[Path]) -> List[Dict[str, Any]]:
    """
    Load data/exception_links.json. Format: list of
      {"base_article_id": <corpus id or null>,
       "exception_article_id": <corpus id>,
       "cited_ref": <id string as printed>, "note": <free text>}
    Entries with a null base are documented-but-unresolved; they emit no edge.
    """
    if path is None or not path.exists():
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ═══════════════════════════════════════════════════════════════════════════
# Graph construction
# ═══════════════════════════════════════════════════════════════════════════

def _upsert_node(G: nx.MultiDiGraph, stats: BuildStats, node_id: str,
                 node_type: str, **attrs: Any) -> str:
    if node_id not in G:
        G.add_node(node_id, node_type=node_type, **attrs)
        stats.node_counts[node_type] += 1
    return node_id


def _add_edge(G: nx.MultiDiGraph, stats: BuildStats, u: str, v: str,
              edge_type: str, _seen: set, **attrs: Any) -> bool:
    """Add a typed edge once; identical (u, v, type, attrs) are deduped."""
    sig = (u, v, edge_type, tuple(sorted((k, json.dumps(val, sort_keys=True))
                                         for k, val in attrs.items())))
    if sig in _seen:
        return False
    _seen.add(sig)
    G.add_edge(u, v, edge_type=edge_type, **attrs)
    stats.edge_counts[edge_type] += 1
    return True


def build_graph(clauses: List[Dict[str, Any]],
                manual_links: Optional[List[Dict[str, Any]]] = None,
                ) -> Tuple[nx.MultiDiGraph, BuildStats]:
    """Build the regulation MultiDiGraph from ingestable clauses."""
    G = nx.MultiDiGraph(name="mabhas_regulation_graph")
    stats = BuildStats()
    seen_edges: set = set()
    ingestable = [c for c in clauses if not c.get("skip_category")]
    clause_ids = [c["article_id"] for c in ingestable]
    id_set = set(clause_ids)

    # ── Pass 1: Clause nodes + metadata-derived edges ──────────────────────
    terms: Dict[str, str] = {}            # term_fa -> defining clause node id
    for c in ingestable:
        rt = c.get("rule_type")
        stats.rule_type_counts[rt] += 1
        cid = _upsert_node(
            G, stats, f"clause:{c['article_id']}", "Clause",
            **{k: c.get(k) for k in CLAUSE_ATTRS},
        )

        for code in c.get("applicable_occupancies") or []:
            oid = _upsert_node(G, stats, f"occupancy:{code}", "Occupancy", code=code)
            _add_edge(G, stats, cid, oid, "APPLIES_TO_OCCUPANCY",
                      seen_edges, source="metadata")

        if rt == "numeric":
            stats.numeric_total += 1
            linked = False
            for ent in _entity_dicts(c):
                raw_obj = ent.get("object")
                canonical = map_element(raw_obj)
                if canonical:
                    stats.observed_elements[canonical] += 1
                    eid = _upsert_node(
                        G, stats, f"element:{canonical}", "Element",
                        canonical_type=canonical,
                        spatial_graph_category=CANONICAL_ELEMENTS[canonical])
                    _add_edge(G, stats, cid, eid, "GOVERNS", seen_edges,
                              raw_text=str(raw_obj), source="metadata")
                    linked = True
                elif raw_obj:
                    stats.unmapped_elements.append(
                        {"article_id": c["article_id"], "field": "numeric.object",
                         "raw": str(raw_obj)})
                prop = map_property(ent.get("property"))
                if prop:
                    pid = _upsert_node(G, stats, f"property:{prop}",
                                       "Property", name=prop)
                    _add_edge(G, stats, cid, pid, "CONSTRAINS_PROPERTY",
                              seen_edges,
                              comparator=ent.get("comparator"),
                              value=ent.get("value"), unit=ent.get("unit"),
                              condition=ent.get("condition"),
                              raw_text=str(ent.get("property")),
                              source="metadata")
                elif ent.get("property"):
                    stats.unmapped_properties.append(
                        {"article_id": c["article_id"],
                         "raw": str(ent.get("property"))})
            if linked:
                stats.numeric_linked += 1

        elif rt == "spatial":
            for ent in _entity_dicts(c):
                for fld, etype in (("subject", "GOVERNS"), ("object", "RELATES")):
                    raw = ent.get(fld)
                    canonical = map_element(raw)
                    if canonical:
                        stats.observed_elements[canonical] += 1
                        eid = _upsert_node(
                            G, stats, f"element:{canonical}", "Element",
                            canonical_type=canonical,
                            spatial_graph_category=CANONICAL_ELEMENTS[canonical])
                        extra = ({"relation": _norm_raw(ent.get("relation")) or None}
                                 if etype == "RELATES" else {})
                        _add_edge(G, stats, cid, eid, etype, seen_edges,
                                  raw_text=str(raw), source="metadata", **extra)
                    elif raw:
                        stats.unmapped_elements.append(
                            {"article_id": c["article_id"],
                             "field": f"spatial.{fld}", "raw": str(raw)})

        elif rt == "definition":
            term = extract_term(c)
            if term:
                tid = _upsert_node(G, stats, f"term:{term}", "Term",
                                   term_fa=term)
                _add_edge(G, stats, cid, tid, "DEFINES", seen_edges,
                          source="derived")
                terms.setdefault(term, cid)
            else:
                stats.definitions_without_term.append(c["article_id"])

    # ── Pass 2: HAS_EXCEPTION (derived resolver + manual table) ────────────
    manual_by_exc: Dict[str, List[Dict[str, Any]]] = {}
    for entry in manual_links or []:
        manual_by_exc.setdefault(entry["exception_article_id"], []).append(entry)

    for c in ingestable:
        if c.get("rule_type") != "exception":
            continue
        stats.exception_total += 1
        exc_node = f"clause:{c['article_id']}"
        resolved_here = False

        manual_entries = manual_by_exc.get(c["article_id"], [])
        manual_refs = {e.get("cited_ref") for e in manual_entries}
        for entry in manual_entries:        # manual table overrides the resolver
            base = entry.get("base_article_id")
            if base and base in id_set:
                _add_edge(G, stats, f"clause:{base}", exc_node, "HAS_EXCEPTION",
                          seen_edges, match_method="manual",
                          cited_ref=entry.get("cited_ref"), source="manual")
                stats.exception_links.append(
                    {"base": base, "exception": c["article_id"],
                     "method": "manual", "cited_ref": entry.get("cited_ref")})
                resolved_here = True

        # entities.applies_to_article, if a future classifier version fills it
        meta_refs = [e.get("applies_to_article") for e in _entity_dicts(c)
                     if e.get("applies_to_article")]
        for ref in meta_refs + parse_article_refs(c):
            if ref in manual_refs:
                continue                    # manually adjudicated above
            matches, method = resolve_reference(ref, clause_ids, c["article_id"])
            if matches:
                for base in matches:
                    _add_edge(G, stats, f"clause:{base}", exc_node,
                              "HAS_EXCEPTION", seen_edges,
                              match_method=method, cited_ref=ref,
                              source="metadata" if ref in meta_refs else "derived")
                    stats.exception_links.append(
                        {"base": base, "exception": c["article_id"],
                         "method": method, "cited_ref": ref})
                resolved_here = True
            else:
                stats.unresolved_exceptions.append(
                    {"article_id": c["article_id"], "cited_ref": ref,
                     "reason": "no unambiguous corpus match"})
        if not meta_refs and not parse_article_refs(c) and not manual_entries:
            stats.unresolved_exceptions.append(
                {"article_id": c["article_id"], "cited_ref": None,
                 "reason": "no article reference in text (excepts a practice); "
                           "needs data/exception_links.json entry"})
        if resolved_here:
            stats.exception_resolved += 1

    # ── Pass 3: USES_TERM (whole-word scan over all clause texts) ──────────
    for term, defining_cid in terms.items():
        if len(term) < 3:
            continue
        pattern = re.compile(
            rf"(?<![\u0600-\u06FF\u200c]){re.escape(term)}(?![\u0600-\u06FF\u200c])")
        tid = f"term:{term}"
        for c in ingestable:
            cid = f"clause:{c['article_id']}"
            if cid == defining_cid:
                continue                    # no self-loop on the definition
            if pattern.search(c.get("text_fa_normalized") or ""):
                _add_edge(G, stats, cid, tid, "USES_TERM", seen_edges,
                          source="derived")

    return G, stats


# ═══════════════════════════════════════════════════════════════════════════
# GraphML round-trip (scalar-only format; lists/None JSON-encoded)
# ═══════════════════════════════════════════════════════════════════════════

_SENTINEL = "__json__:"


def _enc(value: Any) -> Any:
    if value is None or isinstance(value, (list, dict)):
        return _SENTINEL + json.dumps(value, ensure_ascii=False)
    return value


def _dec(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_SENTINEL):
        return json.loads(value[len(_SENTINEL):])
    return value


def save_regulation_graph(G: nx.MultiDiGraph, path: Path) -> None:
    H = nx.MultiDiGraph(name=G.name)
    for n, attrs in G.nodes(data=True):
        H.add_node(n, **{k: _enc(v) for k, v in attrs.items()})
    for u, v, key, attrs in G.edges(keys=True, data=True):
        H.add_edge(u, v, key=key, **{k: _enc(val) for k, val in attrs.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    nx.write_graphml(H, str(path))


def load_regulation_graph(path: Path) -> nx.MultiDiGraph:
    H = nx.read_graphml(str(path), force_multigraph=True)
    G = nx.MultiDiGraph(name=H.name if isinstance(H.name, str) else "")
    for n, attrs in H.nodes(data=True):
        G.add_node(n, **{k: _dec(v) for k, v in attrs.items()})
    for u, v, key, attrs in H.edges(keys=True, data=True):
        G.add_edge(u, v, key=key, **{k: _dec(val) for k, val in attrs.items()})
    return G


# ═══════════════════════════════════════════════════════════════════════════
# Coverage report
# ═══════════════════════════════════════════════════════════════════════════

def write_report(G: nx.MultiDiGraph, stats: BuildStats, path: Path) -> str:
    lines: List[str] = []
    add = lines.append
    add("REGULATION GRAPH — BUILD REPORT")
    add("=" * 60)
    add(f"graph: {G.name} | nodes: {G.number_of_nodes()} | edges: {G.number_of_edges()}")
    add("")
    add("NODES BY TYPE")
    for t, n in sorted(stats.node_counts.items()):
        add(f"  {t:<12} {n}")
    add("")
    add("EDGES BY TYPE")
    for t in EDGE_TYPES:
        add(f"  {t:<22} {stats.edge_counts.get(t, 0)}")
    add("")
    add("INGESTED CLAUSES BY RULE TYPE")
    for t, n in sorted(stats.rule_type_counts.items(), key=lambda kv: -kv[1]):
        add(f"  {t:<12} {n}")
    add("")
    nl, nt = stats.numeric_linked, stats.numeric_total
    add("COVERAGE")
    add(f"  numeric clauses linked to an Element : {nl}/{nt}"
        f" ({(100.0 * nl / nt if nt else 0):.1f}%)")
    er, et = stats.exception_resolved, stats.exception_total
    add(f"  exception clauses resolved           : {er}/{et}"
        f" ({(100.0 * er / et if et else 0):.1f}%)")
    add("")
    add("HAS_EXCEPTION LINKS")
    for l in stats.exception_links:
        add(f"  clause:{l['base']} -> clause:{l['exception']}"
            f"  [{l['method']}; cited '{l['cited_ref']}']")
    for u in stats.unresolved_exceptions:
        add(f"  UNRESOLVED exception {u['article_id']}"
            f" (cited: {u['cited_ref']}) — {u['reason']}")
    add("")
    orphans = [n for n, d in G.nodes(data=True)
               if d.get("node_type") == "Clause" and G.out_degree(n) == 0]
    add(f"CLAUSES WITH NO OUTGOING EDGES ({len(orphans)})")
    for n in sorted(orphans):
        add(f"  {n}")
    add("")
    add("ELEMENT VOCABULARY — observed vs closed schema vocabulary")
    for canonical in sorted(CANONICAL_ELEMENTS):
        add(f"  {canonical:<14} {stats.observed_elements.get(canonical, 0):>4} edge(s)")
    unused = [e for e in CANONICAL_ELEMENTS if stats.observed_elements.get(e, 0) == 0]
    add(f"  (closed-vocabulary types with zero observations: {', '.join(unused) or 'none'})")
    add("")
    add(f"UNMAPPED ELEMENT STRINGS ({len(stats.unmapped_elements)})"
        " — no GOVERNS/RELATES edge emitted (schema policy 4.2)")
    for u in stats.unmapped_elements:
        add(f"  [{u['article_id']}] {u['field']}: {u['raw'][:70]}")
    add("")
    add(f"UNMAPPED PROPERTY STRINGS ({len(stats.unmapped_properties)})")
    for u in stats.unmapped_properties:
        add(f"  [{u['article_id']}] {u['raw'][:70]}")
    add("")
    add(f"DEFINITION CLAUSES WITHOUT EXTRACTABLE TERM"
        f" ({len(stats.definitions_without_term)})")
    for aid in stats.definitions_without_term:
        add(f"  {aid}")
    report = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    return report


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Build the Mabhas regulation graph.")
    p.add_argument("--input", default=None,
                   help="clauses JSON (default: mabhas_clauses_contextual.json "
                        "if present, else mabhas_clauses_normalized.json — "
                        "graph structure is identical either way, since the "
                        "builder reads only metadata fields, not context_fa)")
    p.add_argument("--output", default="data/regulation_graph.graphml")
    p.add_argument("--report", default="docs/regulation_graph_report.txt")
    p.add_argument("--manual-links", default="data/exception_links.json")
    p.add_argument("--unmapped-json",
                   default="eval/results/graph_unmapped_entities.json",
                   help="machine-readable dump of unmapped entity strings")
    args = p.parse_args(argv)

    if args.input is None:
        # Inline fallback (no services.* import — this script runs standalone
        # via `python -m rag.build_regulation_graph` outside the alias
        # bootstrap). Graph structure is identical from either file: the
        # builder reads only metadata fields, never context_fa.
        _preferred = "data/mabhas_clauses_contextual.json"
        _fallback = "data/mabhas_clauses_normalized.json"
        args.input = _preferred if Path(_preferred).exists() else _fallback
        if args.input == _fallback:
            print(f"[build_regulation_graph] {_preferred} not found — "
                  f"using {_fallback} (equivalent for graph building)")

    with open(args.input, encoding="utf-8") as fh:
        clauses = json.load(fh)
    manual = load_manual_links(Path(args.manual_links))

    G, stats = build_graph(clauses, manual)
    save_regulation_graph(G, Path(args.output))

    unmapped_path = Path(args.unmapped_json)
    unmapped_path.parent.mkdir(parents=True, exist_ok=True)
    unmapped_path.write_text(json.dumps(
        {"elements": stats.unmapped_elements,
         "properties": stats.unmapped_properties},
        ensure_ascii=False, indent=2), encoding="utf-8")

    report = write_report(G, stats, Path(args.report))
    sys.stdout.write(report)
    print(f"graph   -> {args.output}")
    print(f"report  -> {args.report}")
    print(f"unmapped-> {args.unmapped_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
