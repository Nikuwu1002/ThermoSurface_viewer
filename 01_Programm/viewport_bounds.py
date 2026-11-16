"""TS-VP04: Viewportzustand je Diagramm (VTK-/Qt-frei).

Ein manueller Viewport begrenzt NUR die gerenderte Darstellung. Er veraendert weder das Ergebnis-Dictionary noch den
Cache-Schluessel und loest keine HEOS-Berechnung aus. Intern gelten immer SI-Einheiten; die GUI-Felder sind in
Anzeigeeinheiten (siehe diagram_axes.AxisSpec) und werden erst durch ausdrueckliches "Ansicht anwenden" wirksam.
"""
import math
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional

import diagram_axes as axes
from diagram_axes import ViewportBoundsError


def parse_field(text, name="Wert"):
    """GUI-Text -> float; akzeptiert Dezimalkomma und Exponentialschreibweise, wirft ViewportBoundsError."""
    if text is None:
        raise ViewportBoundsError(f"{name}: keine Eingabe.")
    cleaned = str(text).strip().replace(",", ".").replace(" ", "")
    if not cleaned:
        raise ViewportBoundsError(f"{name}: keine Eingabe.")
    try:
        value = float(cleaned)
    except ValueError:
        raise ViewportBoundsError(f"{name}: '{text}' ist keine Zahl.") from None
    if not math.isfinite(value):
        raise ViewportBoundsError(f"{name}: Wert muss endlich sein.")
    return value


@dataclass
class ViewportState:
    """Viewportzustand eines Diagramms.

    enabled      manuelle Begrenzung wirksam (nur nach "Ansicht anwenden")
    bounds       {Groesse: (min_si, max_si)} in Reihenfolge der Darstellungsachsen x, y, z
    fields       letzte gueltige Eingabetexte [x_von, x_bis, y_von, y_bis, z_von, z_bis] in Anzeigeeinheiten
    camera_state Kamera, getrennt vom Darstellungsfenster gespeichert (Standardansicht bleibt wiederherstellbar)
    user_defined True, sobald der Benutzer einmal gueltige Grenzen angewendet hat
    """
    diagram_id: str
    enabled: bool = False
    bounds: dict = field(default_factory=dict)
    fields: Optional[list] = None
    camera_state: Optional[list] = None
    user_defined: bool = False

    def copy(self):
        return deepcopy(self)

    @property
    def bounds_si(self):
        """[(lo, hi)_x, (lo, hi)_y, (lo, hi)_z] in SI oder None."""
        if not self.bounds:
            return None
        return [self.bounds[spec.key] for spec in axes.axes_for(self.diagram_id)]

    def apply_fields(self, texts, daten):
        """Prueft die sechs Eingabetexte und uebernimmt sie atomar; bei Fehler bleibt der alte Zustand unveraendert.

        Rueckgabe: VTK-Box (xmin, xmax, ymin, ymax, zmin, zmax). Wirft ViewportBoundsError.
        """
        specs = axes.axes_for(self.diagram_id)
        if len(texts) != 6:
            raise ViewportBoundsError("Es sind sechs Grenzen (Start/Stop je Achse) erforderlich.")
        pairs = []
        for index, spec in enumerate(specs):
            letter = axes.AXIS_LETTERS[index]
            lo = parse_field(texts[2 * index], f"{letter}-Achse ({spec.key}) Start")
            hi = parse_field(texts[2 * index + 1], f"{letter}-Achse ({spec.key}) Stop")
            pairs.append((lo, hi))
        bounds_si, box = axes.validate_bounds(self.diagram_id, daten, pairs)
        self.bounds = {spec.key: pair for spec, pair in zip(specs, bounds_si)}
        self.fields = [str(t).strip() for t in texts]
        self.enabled = True
        self.user_defined = True
        return box

    def box(self, daten):
        """Aktuelle VTK-Box fuer `daten` oder None, wenn die manuelle Begrenzung nicht wirksam ist."""
        if not (self.enabled and self.bounds):
            return None
        return axes.bounds_to_box(self.diagram_id, daten, self.bounds_si)

    def reset(self):
        """Schaltet die manuelle Begrenzung aus; die letzten Eingaben bleiben als Vorschlag erhalten."""
        self.enabled = False
