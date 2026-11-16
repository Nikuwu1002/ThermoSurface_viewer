"""Registry der 3D-Diagrammtypen (TS-A02).

Ein neues Diagramm wird hier einmal registriert; Dropdown, Checkboxen, Berechnung
(`main.berechne_auswahl`), Cache-Schluessel und Viewport nutzen ausschliesslich die Registry.
Dieses Modul importiert weder Qt noch PyVista noch CoolProp (Renderer werden lazy aufgeloest).
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

# Reihenfolge = Reihenfolge der Checkboxen in der GUI.
LAYER_LABELS = {
    "surface": "Zustandsoberfläche",
    "isothermen": "Isothermen",
    "isobaren": "Isobaren",
    "isochoren": "Isochoren",
    "binodale": "Siede- und Taulinie",
    "spinodale": "Spinodale",
    "zweiphasengebiet": "Zweiphasengebiet",
    "metastable_liquid_surface": "Metastabile Flüssigkeit (homogen)",
    "metastable_vapor_surface": "Metastabiler Dampf (homogen)",
    "unstable_surface": "Instabile homogene Fortsetzung (HEOS, gefiltert)",
    "replacement_surface": "Ersatzfläche (Modell, keine EOS-Auswertung)",
    "critical_point": "Kritischer Punkt",
}

BASE_LAYERS = ("surface", "binodale", "zweiphasengebiet", "critical_point")
ISOLINE_LAYERS = ("isothermen", "isobaren", "isochoren")
PVT_LAYERS = tuple(LAYER_LABELS)
BASE_DEFAULT_ON = frozenset(BASE_LAYERS)
PVT_DEFAULT_OFF = frozenset({"spinodale", "zweiphasengebiet", "unstable_surface"})

# Schreibt ein Renderer-Ergebnis fuer die Statuszeile / Stoffinfo (reine Formatierung).
InfoFormatter = Callable[[dict, str], str]


def _info(fmt):
    def build(daten, stoff):
        meta = daten["meta"]
        return f"{stoff}: " + fmt(meta)
    return build


@dataclass
class DiagramSpec:
    id: str
    label: str
    render_name: str
    layers: tuple
    default_layers: frozenset
    confirm_label: str = ""
    info: Optional[InfoFormatter] = None
    hover: bool = False
    # Von main.py angehaengt (Berechnung liegt in main.BERECHNUNGEN, siehe dort).
    cache_params: Optional[Callable] = field(default=None, repr=False)

    def render(self, plotter, daten):
        import visualization
        return getattr(visualization, self.render_name)(plotter, daten)

    def initial_layer_state(self):
        return {key: key in self.default_layers for key in LAYER_LABELS}

    def info_text(self, daten, stoff):
        return self.info(daten, stoff) if self.info else ""


class DiagramRegistry:
    def __init__(self):
        self._specs = {}

    def register(self, spec):
        if spec.id in self._specs:
            raise ValueError(f"Diagramm {spec.id!r} ist bereits registriert.")
        unknown = set(spec.layers) - set(LAYER_LABELS)
        if unknown:
            raise ValueError(f"Unbekannte Layer fuer {spec.id}: {sorted(unknown)}")
        self._specs[spec.id] = spec
        return spec

    def attach(self, diagram_id, cache_params=None):
        spec = self.get(diagram_id)
        if cache_params is not None:
            spec.cache_params = cache_params

    def get(self, diagram_id):
        try:
            return self._specs[diagram_id]
        except KeyError:
            raise ValueError(f"Unbekannter Diagrammtyp: {diagram_id}") from None

    def ids(self):
        return list(self._specs)

    def __contains__(self, diagram_id):
        return diagram_id in self._specs

    def __iter__(self):
        return iter(self._specs.values())

    def __len__(self):
        return len(self._specs)


DIAGRAMS = DiagramRegistry()

DIAGRAMS.register(DiagramSpec(
    id="pvt", label="p-v-T (3D)", render_name="render_pvt", layers=PVT_LAYERS,
    default_layers=frozenset(set(PVT_LAYERS) - PVT_DEFAULT_OFF), confirm_label="p-v-T", hover=True))
DIAGRAMS.register(DiagramSpec(
    id="pht", label="p-h-T (3D)", render_name="render_pht", layers=BASE_LAYERS + ISOLINE_LAYERS,
    default_layers=BASE_DEFAULT_ON, confirm_label="p-h-T",
    info=_info(lambda m: f"Tc={m['Tc']:.3f} K, pc={m['pc']/1e6:.3f} MPa, hc={m['hc']/1e3:.3f} kJ/kg")))
DIAGRAMS.register(DiagramSpec(
    id="pst", label="p-s-T (3D)", render_name="render_pst", layers=BASE_LAYERS + ISOLINE_LAYERS,
    default_layers=BASE_DEFAULT_ON, confirm_label="p-s-T",
    info=_info(lambda m: f"Tc={m['Tc']:.3f} K, pc={m['pc']/1e6:.3f} MPa, sc={m['sc']/1e3:.3f} kJ/(kg K)")))
DIAGRAMS.register(DiagramSpec(
    id="hsp", label="h-s-p (3D)", render_name="render_hsp", layers=BASE_LAYERS + ISOLINE_LAYERS,
    default_layers=BASE_DEFAULT_ON, confirm_label="h-s-p",
    info=_info(lambda m: (f"Tc={m['Tc']:.3f} K, pc={m['pc']/1e6:.3f} MPa, hc={m['hc']/1e3:.3f} kJ/kg, "
                          f"sc={m['sc']/1e3:.3f} kJ/(kg K)"))))
DIAGRAMS.register(DiagramSpec(
    id="uvt", label="u-v-T (3D)", render_name="render_uvt", layers=BASE_LAYERS + ISOLINE_LAYERS,
    default_layers=BASE_DEFAULT_ON, confirm_label="u-v-T",
    info=_info(lambda m: f"Tc={m['Tc']:.3f} K, vc={m['vc']:.8g} m³/mol, uc={m['uc']/1e3:.3f} kJ/kg")))

# g-p-T / f-v-T: homogene Ebenen (metastabil/instabil) und Spinodalen sind optionale Ebenen und standardmaessig aus,
# damit die erste Ansicht die reine Gleichgewichtsflaeche zeigt. g-p-T hat keine Zweiphasenflaeche (g_l = g_g:
# das Zweiphasengebiet entartet zur Saettigungslinie, die als "Siede- und Taulinie" dargestellt wird).
HOMOGENEOUS_LAYERS = ("spinodale", "metastable_liquid_surface", "metastable_vapor_surface", "unstable_surface")
GPT_LAYERS = tuple(k for k in BASE_LAYERS if k != "zweiphasengebiet") + ISOLINE_LAYERS + HOMOGENEOUS_LAYERS
FVT_LAYERS = BASE_LAYERS + ISOLINE_LAYERS + HOMOGENEOUS_LAYERS


def _gpt_info(daten, stoff):
    meta = daten["meta"]
    text = f"{stoff}: Tc={meta['Tc']:.3f} K, pc={meta['pc']/1e6:.3f} MPa, gc={meta['gc']/1e3:.3f} kJ/kg"
    notice = daten.get("pressure_axis_notice")
    return f"{text}. {notice}" if notice else text


DIAGRAMS.register(DiagramSpec(
    id="gpt", label="g-p-T (3D)", render_name="render_gpt", layers=GPT_LAYERS,
    default_layers=frozenset({"surface", "binodale", "critical_point"}), confirm_label="g-p-T", info=_gpt_info))
DIAGRAMS.register(DiagramSpec(
    id="fvt", label="f-v-T (3D)", render_name="render_fvt", layers=FVT_LAYERS,
    default_layers=BASE_DEFAULT_ON, confirm_label="f-v-T",
    info=_info(lambda m: f"Tc={m['Tc']:.3f} K, vc={m['vc']:.8g} m³/mol, fc={m['fc']/1e3:.3f} kJ/kg")))
