"""
utils/polygon_geometry.py
=========================
Pure-Python geometry helpers for polygons expressed in millimetres.

Functions
---------
polygon_area_m2(polygon_mm)        → area in square metres
polygon_perimeter_m(polygon_mm)    → perimeter in metres

Both functions accept the same input format used throughout the BIM pipeline:
a list of [x_mm, y_mm] pairs. The polygon may be open (first ≠ last) or
explicitly closed (first == last); either is handled correctly.

Design choices
--------------
- No numpy dependency. These helpers are called once per element in /analyze;
  the loop overhead of pure Python is negligible (microseconds), and avoiding
  numpy makes the functions safe to call from any context, including ones
  that might run before numpy is imported.

- Outputs are in SI units (m, m²) because that is what BIM tools and the
  client UI display to end users. Internally the polygons are stored in mm
  to preserve sub-millimetre precision for IFC export; the conversion to m
  happens only at the API boundary.

- Both functions are total: they never raise on degenerate input (empty list,
  single point, collinear points). They return 0.0 for polygons that don't
  enclose a real area. This keeps callers simple — no try/except needed
  around routine geometry computation.

- All inputs are coerced to float to defend against numpy scalars sneaking
  in from upstream callers.
"""

from __future__ import annotations

from typing import List, Sequence


# ─────────────────────────────────────────────────────────────────────────────
# Area
# ─────────────────────────────────────────────────────────────────────────────

def polygon_area_m2(polygon_mm: Sequence[Sequence[float]]) -> float:
    """
    Compute polygon area in square metres using the shoelace formula.

    Parameters
    ----------
    polygon_mm : list of [x_mm, y_mm]
        Vertices in millimetres, in any consistent order (clockwise or
        counter-clockwise — the absolute value of the signed area is returned).
        The polygon may be open (n vertices) or closed (n+1 vertices with the
        first repeated at the end); both produce the same result.

    Returns
    -------
    float
        Area in m². Always ≥ 0. Returns 0.0 for degenerate input (< 3 vertices).
    """
    if polygon_mm is None or len(polygon_mm) < 3:
        return 0.0

    # If the polygon is closed, drop the duplicated last vertex so the
    # shoelace loop doesn't double-count the closing edge.
    pts = list(polygon_mm)
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]

    if len(pts) < 3:
        return 0.0

    area_mm2 = 0.0
    n = len(pts)
    for i in range(n):
        j = (i + 1) % n
        x_i, y_i = float(pts[i][0]), float(pts[i][1])
        x_j, y_j = float(pts[j][0]), float(pts[j][1])
        area_mm2 += x_i * y_j - x_j * y_i
    area_mm2 = abs(area_mm2) / 2.0

    # 1 m² = 1,000,000 mm²
    return area_mm2 / 1_000_000.0


# ─────────────────────────────────────────────────────────────────────────────
# Perimeter
# ─────────────────────────────────────────────────────────────────────────────

def polygon_perimeter_m(polygon_mm: Sequence[Sequence[float]]) -> float:
    """
    Compute polygon perimeter in metres.

    Parameters
    ----------
    polygon_mm : list of [x_mm, y_mm]
        Vertices in millimetres. May be open or closed. If open, the closing
        edge from the last vertex back to the first is included automatically.

    Returns
    -------
    float
        Perimeter in m. Always ≥ 0. Returns 0.0 for input with fewer than 2
        distinct vertices.
    """
    if polygon_mm is None or len(polygon_mm) < 2:
        return 0.0

    pts = list(polygon_mm)

    # If the polygon is NOT explicitly closed, add the closing edge by
    # treating the loop modulo n. If it IS explicitly closed, the last
    # segment from pts[n-1] to pts[0] would double-count, so drop the
    # duplicated last vertex first.
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]

    if len(pts) < 2:
        return 0.0

    perim_mm = 0.0
    n = len(pts)
    for i in range(n):
        j = (i + 1) % n
        dx = float(pts[j][0]) - float(pts[i][0])
        dy = float(pts[j][1]) - float(pts[i][1])
        perim_mm += (dx * dx + dy * dy) ** 0.5

    # 1 m = 1000 mm
    return perim_mm / 1000.0
