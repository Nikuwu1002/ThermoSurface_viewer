"""TS-B03: Punkt-Picking fuer alle Diagrammtypen (geometrische Lokalisierung, keine Thermodynamik).

`pick_in_cell` ist rein numerisch und testbar; `pick_from_viewport` bindet den VTK-Zellpicker an.
Baryzentrische Gewichte dienen ausschliesslich der Lokalisierung; die Thermodynamik rekonstruiert
`state_resolver.py` anschliessend ueber HEOS.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from state_store import (CRITICAL, HOMOGENEOUS, SATURATION, SINGLE_PHASE, SUBSTITUTE, TWO_PHASE, NodeRef, format_ref)

LOGGER = logging.getLogger("thermosurface.picker")

CLIPPED_CELL_ARRAY = "ts_orig_cell"   # Zellarray geclippter Netze (viewport_clipping.ORIG_CELL)

EXACT_NODE_WEIGHT = 1.0 - 1e-6   # Gewicht ab dem ein Treffer als exakter Gitterknoten gilt

# Actor-Schluessel, die echte Zustandsflaechen darstellen (Kurven, kritischer Punkt, Ticks sind nicht pickbar).
PICKABLE_ACTORS = ("surface", "zweiphasengebiet", "metastable_liquid_surface", "metastable_vapor_surface",
                   "unstable_surface", "replacement_surface")

# source_class
CLASS_HEOS = "heos"
CLASS_TWO_PHASE = "two_phase"
CLASS_HOMOGENEOUS = "homogeneous"
CLASS_SUBSTITUTE = "synthetic_substitute"


@dataclass
class MeshPick:
    diagram_id: str
    actor_key: str
    cell_id: int
    source_point_ids: list                  # 3 x NodeRef | None
    weights: tuple                          # barycentrische Gewichte (Summe 1)
    display_xyz: tuple
    physical_hint: dict = field(default_factory=dict)
    source_class: str = CLASS_HEOS
    fluid: str = ""
    sub_index: Optional[int] = None
    message: str = ""
    clipped: bool = False                   # True: Treffer auf einem am Ansichtsbereich beschnittenen Netz

    @property
    def is_exact_node(self):
        return max(self.weights) >= EXACT_NODE_WEIGHT

    @property
    def exact_ref(self):
        if not self.is_exact_node:
            return None
        return self.source_point_ids[int(np.argmax(self.weights))]

    @property
    def fully_located(self):
        return all(ref is not None for ref in self.source_point_ids)

    def point_ids(self):
        return [format_ref(r) if r is not None else None for r in self.source_point_ids]


class PickError(ValueError):
    """Treffer nicht auswertbar (degeneriertes Dreieck, nicht pickbarer Actor, unbekannte Quellknoten)."""


def barycentric_weights(triangle_xyz, position):
    """Baryzentrische Koordinaten von `position` bezueglich des Dreiecks; auf die Ebene projiziert."""
    a, b, c = (np.asarray(p, dtype=float) for p in triangle_xyz)
    p = np.asarray(position, dtype=float)
    v0, v1, v2 = b - a, c - a, p - a
    d00, d01, d11 = v0 @ v0, v0 @ v1, v1 @ v1
    d20, d21 = v2 @ v0, v2 @ v1
    denom = d00 * d11 - d01 * d01
    if not np.isfinite(denom) or abs(denom) <= 1e-30 * max(d00 * d11, 1e-300):
        raise PickError("Degeneriertes Dreieck: keine eindeutigen Gewichte")
    w1 = (d11 * d20 - d01 * d21) / denom
    w2 = (d00 * d21 - d01 * d20) / denom
    w = np.array([1.0 - w1 - w2, w1, w2])
    if (w < -1e-3).any():
        raise PickError("Position liegt ausserhalb des Dreiecks")
    w = np.clip(w, 0.0, None)
    return tuple(float(x) for x in w / w.sum())


def classify(index, refs):
    """Quellklasse des Treffers aus den Flaechenarten der Eckknoten."""
    kinds = [index.surface(r.surface).kind for r in refs if r is not None]
    main = [k for k in kinds if k not in (SATURATION, CRITICAL)]
    kind = main[0] if main else (kinds[0] if kinds else SINGLE_PHASE)
    return {TWO_PHASE: CLASS_TWO_PHASE, HOMOGENEOUS: CLASS_HOMOGENEOUS, SUBSTITUTE: CLASS_SUBSTITUTE}.get(kind, CLASS_HEOS)


def pick_in_cell(index, actor_key, sub_index, cell_xyz, position, cell_id=-1):
    """Lokalisiert `position` im Dreieck `cell_xyz` (3x3 Darstellungskoordinaten) und ordnet Quellknoten zu."""
    if actor_key not in PICKABLE_ACTORS:
        raise PickError(f"Actor {actor_key!r} ist keine pickbare Zustandsflaeche")
    cell_xyz = np.asarray(cell_xyz, dtype=float)
    if cell_xyz.shape != (3, 3):
        raise PickError("Nur Dreieckszellen sind pickbar")
    weights = barycentric_weights(cell_xyz, position)
    located = index.locate(actor_key, sub_index, cell_xyz)
    refs = [ref for ref, _ in located]
    adapter = index.adapter
    hint = {
        "axes": adapter.axes,
        "native_interpolated": tuple(float(x) for x in adapter.from_display(np.asarray(position, dtype=float), index.daten)),
        "vertex_native": [index.native(r).tolist() if r is not None else None for r in refs],
        "vertex_distance": [float(d) for _, d in located],
        "vertex_kinds": [index.surface(r.surface).kind if r is not None else None for r in refs],
        "vertex_phase": [index.surface(r.surface).phase if r is not None else None for r in refs],
        "vertex_source": [index.surface(r.surface).node_source(r.index) if r is not None else None for r in refs],
        "vertex_aux_v": [index.surface(r.surface).node_aux("v", r.index) if r is not None else None for r in refs],
    }
    pick = MeshPick(index.diagram_id, actor_key, int(cell_id), refs, weights, tuple(float(x) for x in position),
                    physical_hint=hint, fluid=index.fluid, sub_index=sub_index)
    if all(r is not None for r in refs):
        pick.source_class = classify(index, refs)
    else:
        pick.message = "Mindestens ein Dreiecksknoten ist keinem Quellknoten zuordenbar"
    return pick


# ---------------------------------------------------------------- VTK-Anbindung

def _same(a, b):
    if a is b:
        return True
    try:
        return a.GetAddressAsString("") == b.GetAddressAsString("")
    except Exception:
        return False


def find_actor(actors, picked_actor, picked_mapper=None):
    """(actor_key, sub_index) des getroffenen Actors innerhalb des Actor-Dictionaries eines Viewports."""
    for key, objects in actors.items():
        if objects is None:
            continue
        listed = isinstance(objects, (list, tuple))
        for i, obj in enumerate(objects if listed else [objects]):
            if obj is None:
                continue
            if _same(obj, picked_actor) or (picked_mapper is not None and _same(obj.GetMapper(), picked_mapper)):
                return key, (i if listed else None)
    return None, None


def pick_from_viewport(viewport, index, x, y):
    """Pickt am Displaypunkt (x, y) im Viewport (VTK-Koordinaten). Rueckgabe MeshPick oder wirft PickError."""
    from vtkmodules.vtkRenderingCore import vtkCellPicker
    plotter = viewport.plotter
    picker = vtkCellPicker()
    picker.SetTolerance(0.005)
    if not picker.Pick(x, y, 0, plotter.renderer):
        raise PickError("Kein Treffer auf einer Flaeche")
    key, sub_index = find_actor(viewport.actors, picker.GetActor(), picker.GetMapper())
    if key is None or key not in PICKABLE_ACTORS:
        raise PickError("Treffer liegt nicht auf einer pickbaren Zustandsflaeche (Kurve/Punkt/Beschriftung)")
    data = picker.GetMapper().GetInput()
    cell_id = picker.GetCellId()
    cell = data.GetCell(cell_id)
    if cell is None or cell.GetNumberOfPoints() != 3:
        raise PickError("Getroffene Zelle ist kein Dreieck")
    original_cells = data.GetCellData().GetArray(CLIPPED_CELL_ARRAY)
    if original_cells is not None:
        # Beschnittenes Netz (TS-VP05): Schnittpunkte sind Renderer-Interpolationen und keine HEOS-Knoten. Die
        # Zustandsrekonstruktion geht daher vom unveraenderten Ursprungsdreieck aus (die Pickposition liegt darin).
        triangle = viewport.clip_registry.base_triangle(picker.GetActor(), int(original_cells.GetValue(cell_id)))
        if triangle is None:
            raise PickError("Beschnittene Zelle ist keinem Ursprungsdreieck zuordenbar")
        xyz = [tuple(float(c) for c in point) for point in triangle]
    else:
        xyz = [data.GetPoint(cell.GetPointId(k)) for k in range(3)]
    pick = pick_in_cell(index, key, sub_index, xyz, picker.GetPickPosition(), cell_id)
    pick.clipped = original_cells is not None
    return pick
