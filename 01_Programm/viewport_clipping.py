"""TS-VP05: PyVista-Anbindung des geometrischen Clippings (Actors eines Viewports).

Die Originalgeometrie jedes Actors wird beim Registrieren festgehalten; jedes Clipping geht vom Original aus (kein
kumulatives Wegschneiden). Geclippte Netze tragen die Zellgroesse `ts_orig_cell` (Index des Ursprungsdreiecks) und
neue Schnittpunkte haben `source = SRC_CLIPPED`, d. h. sie sind Renderer-Interpolationen und keine HEOS-Knoten.
Es wird nichts neu berechnet; Berechnungsarrays und Ergebnis-Dictionary bleiben unveraendert.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pyvista as pv

import geometry_clip as gc
from zustandsflaeche import SRC_CLIPPED

LOGGER = logging.getLogger("thermosurface.clipping")

ORIG_CELL = "ts_orig_cell"
SKIP_KEYS = ("pressure_ticks",)


def as_polydata(mesh):
    """Oberflaechen-PolyData (Dreiecke, Linien, Punkte) eines beliebigen PyVista-Datensatzes."""
    pd = mesh if isinstance(mesh, pv.PolyData) else mesh.extract_surface()
    if pd.n_cells and pd.faces.size and not pd.is_all_triangles:
        pd = pd.triangulate()
    return pd


def _cells(flat):
    """Flaches VTK-Zellarray [n, i0, ..., n, ...] -> Liste von Indexarrays."""
    flat = np.asarray(flat, dtype=np.int64)
    out, pos = [], 0
    while pos < len(flat):
        count = int(flat[pos])
        out.append(flat[pos + 1:pos + 1 + count])
        pos += count + 1
    return out


def _flat(cells):
    if not len(cells):
        return None
    return np.concatenate([np.concatenate(([len(c)], c)) for c in cells]).astype(np.int64)


@dataclass
class _Entry:
    actor: Any
    original: Any
    base: Any = None
    triangles: Optional[np.ndarray] = None
    points: Optional[np.ndarray] = None
    key: str = ""


class ClipRegistry:
    """Verwaltet Originale und geclippte Darstellungen der Actors eines Viewports."""

    def __init__(self):
        self.entries = []
        self._by_id = {}
        self.box = None

    # ------------------------------------------------------------ Registrierung
    def register(self, actors):
        self.entries, self._by_id, self.box = [], {}, None
        for key, objects in (actors or {}).items():
            if key in SKIP_KEYS or objects is None:
                continue
            for actor in (objects if isinstance(objects, (list, tuple)) else [objects]):
                if actor is None:
                    continue
                dataset = self._dataset(actor)
                if dataset is None:
                    continue
                entry = _Entry(actor=actor, original=dataset, key=key)
                self.entries.append(entry)
                self._by_id[id(actor)] = entry

    @staticmethod
    def _dataset(actor):
        mapper = actor.GetMapper() if hasattr(actor, "GetMapper") else None
        if mapper is None:
            return None
        data = mapper.GetInput()
        return pv.wrap(data) if data is not None else None

    def _prepare(self, entry):
        if entry.base is None:
            entry.base = as_polydata(entry.original)
            entry.points = np.asarray(entry.base.points, dtype=float)
            faces = entry.base.faces
            entry.triangles = faces.reshape(-1, 4)[:, 1:] if faces.size else np.empty((0, 3), dtype=np.int64)
        return entry

    # ------------------------------------------------------------ Zugriff fuer Picker
    def base_triangle(self, actor, original_cell_id):
        """(3,3)-Koordinaten des Ursprungsdreiecks eines geclippten Netzes oder None (nicht registriert/nicht geclippt)."""
        entry = self._by_id.get(id(actor))
        if entry is None or entry.base is None or entry.triangles is None:
            return None
        if not 0 <= original_cell_id < len(entry.triangles):
            return None
        return entry.points[entry.triangles[original_cell_id]]

    def is_clipped(self, actor):
        entry = self._by_id.get(id(actor))
        return entry is not None and self.box is not None

    # ------------------------------------------------------------ Anwenden
    def apply(self, box, new_point_values=None):
        """Setzt `box` (VTK-Koordinaten) oder stellt bei None die Originale wieder her.

        new_point_values(xyz) -> {Arrayname: Werte} fuer neue Schnittpunkte (aus der Rueckwaertstransformation der
        Darstellungskoordinaten; keine HEOS-Auswertung). Rueckgabe: Statistik {"actors", "empty", "box"}.
        """
        self.box = None if box is None else tuple(float(x) for x in box)
        stats = {"actors": len(self.entries), "empty": 0, "box": self.box}
        for entry in self.entries:
            if self.box is None:
                dataset = entry.original
            else:
                dataset = self._clip(self._prepare(entry), new_point_values)
                if dataset.n_points == 0:
                    stats["empty"] += 1
            entry.actor.GetMapper().SetInputData(dataset)
        return stats

    def _clip(self, entry, new_point_values):
        base = entry.base
        pts = entry.points
        if not len(pts) or not base.n_cells:
            return pv.PolyData()
        out_points = []
        faces_out, lines_out, verts_out = None, None, None
        offset = 0
        extras = {}

        if len(entry.triangles):
            arrays = {name: np.asarray(base.point_data[name]) for name in base.point_data.keys()
                      if np.asarray(base.point_data[name]).ndim == 1}
            result = gc.clip_triangles(pts, entry.triangles, self.box, arrays)
            if len(result["triangles"]):
                new_xyz = result["points"]
                data = result["point_data"]
                is_new = result["new"]
                if "source" in data:
                    data["source"] = np.where(is_new, SRC_CLIPPED, np.rint(data["source"])).astype(base.point_data["source"].dtype)
                if new_point_values is not None and is_new.any():
                    for name, values in (new_point_values(new_xyz[is_new]) or {}).items():
                        if name in data:
                            data[name] = data[name].copy()
                            data[name][is_new] = values
                for name in list(data):
                    if name != "source" and base.point_data[name].dtype.kind in "iu":
                        data[name] = np.rint(data[name]).astype(base.point_data[name].dtype)
                faces_out = np.column_stack((np.full(len(result["triangles"]), 3, dtype=np.int64),
                                             result["triangles"] + offset)).ravel()
                out_points.append(new_xyz)
                offset += len(new_xyz)
                extras = {"point_data": data, "orig_cell": result["orig_cell"]}

        if base.n_lines:
            lines = _cells(base.lines)
            result = gc.clip_polylines(pts, lines, self.box)
            if len(result["lines"]):
                out_points.append(result["points"])
                lines_out = _flat([ln + offset for ln in result["lines"]])
                offset += len(result["points"])

        if base.n_verts:
            verts = np.concatenate(_cells(base.verts)) if base.verts.size else np.empty(0, dtype=np.int64)
            keep = verts[gc.points_in_box(pts[verts], self.box)]
            if len(keep):
                out_points.append(pts[keep])
                verts_out = _flat([np.array([offset + i]) for i in range(len(keep))])
                offset += len(keep)

        if not out_points:
            return pv.PolyData()

        points = np.vstack(out_points)
        kwargs = {}
        if faces_out is not None:
            kwargs["faces"] = faces_out
        if lines_out is not None:
            kwargs["lines"] = lines_out
        if verts_out is not None:
            kwargs["verts"] = verts_out
        mesh = pv.PolyData(points, **kwargs)

        if faces_out is not None:
            # Punktdaten: nur fuer den Flaechenanteil (Punkte stehen am Anfang des gemeinsamen Punktarrays).
            n_face_points = len(out_points[0])
            for name, values in extras["point_data"].items():
                full = np.zeros(len(points), dtype=values.dtype)
                full[:n_face_points] = values
                mesh.point_data[name] = full
            cell_offset = base.n_verts + base.n_lines
            first_face_cell = mesh.n_verts + mesh.n_lines
            orig = np.full(mesh.n_cells, -1, dtype=np.int64)
            orig[first_face_cell:first_face_cell + len(extras["orig_cell"])] = extras["orig_cell"]
            mesh.cell_data[ORIG_CELL] = orig
            for name in base.cell_data.keys():
                column = np.asarray(base.cell_data[name])
                if column.ndim == 1:
                    values = np.zeros(mesh.n_cells, dtype=column.dtype)
                    values[first_face_cell:first_face_cell + len(extras["orig_cell"])] = \
                        column[cell_offset + extras["orig_cell"]]
                    mesh.cell_data[name] = values
            if "Normals" in base.point_data.keys():
                mesh = mesh.compute_normals(cell_normals=False, point_normals=True, split_vertices=False,
                                            auto_orient_normals=False, consistent_normals=False, inplace=False)
        return mesh
