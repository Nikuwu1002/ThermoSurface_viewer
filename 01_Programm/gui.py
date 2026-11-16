"""PySide6-GUI: Stoff- und Rasterwahl, 3D-Viewport, Ebenensichtbarkeit.

Berechnungen erfolgen ausschließlich über den Callback aus main.py.
"""

"""PySide6-GUI: Stoff- und Rasterwahl, Diagramm-Workspace (Einzel-/Mehrfachansicht), Ebenensichtbarkeit.

Berechnungen erfolgen ausschließlich über den Callback aus main.py (CalculationManager, sequenziell im QThread).
"""

from zustandsflaeche import HOMOGENEOUS_CHOICE, resolve_homogeneous_choice, export_homogeneous_csv
import numpy as np
import logging
from limits import MAX_POINTS, CACHE_MAX_BYTES, CACHE_WARN_BYTES, STATE_CACHE
from diagram_registry import DIAGRAMS, LAYER_LABELS
from diagram_cache import DiagramResultCache
from calculation_manager import CalculationManager, CalculationWorker, DiagramTask
from diagram_workspace import DiagramWorkspace
from state_store import StateStore, StateStoreConfig
from state_resolver import StateResolver
from state_picker import pick_from_viewport, PickError
from selection_manager import SelectionManager, FluidMismatch, NotAddable
from thermo_state import SOURCE_AXIS_UNITS
import diagram_axes
from diagram_axes import ViewportBoundsError
from isoline_levels import (IsolineLevelError, FAMILIES as ISOLINE_FAMILIES, FACTOR_DEFAULT, generate_levels,
                            normalise_isoline_config)
from viewport_bounds import ViewportState

LOGGER = logging.getLogger("thermosurface.gui")
from copy import deepcopy
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import(  QMainWindow, 
                                QWidget, 
                                QVBoxLayout, 
                                QSplitter, 
                                QLabel,
                                QComboBox,
                                QCheckBox,
                                QPushButton,
                                QGroupBox,
                                QFormLayout,
                                QDoubleSpinBox,
                                QSpinBox,
                                QLineEdit,
                                QScrollArea,
                                QMessageBox,
                                QFileDialog,
                                QSizePolicy,
                                QProgressBar,
                                QListWidget,
                                QListWidgetItem,
                                QHBoxLayout,)


MODE_SINGLE = "single"
MODE_MULTI = "multi"

# Isolinienfamilien in der GUI: (Familie, Titel, Einheit, Faktor GUI->SI, Nachkommastellen)
ISOLINE_GUI_FAMILIES = (
    ("isothermen", "Isothermen T", "K", 1.0, 6),
    ("isochoren", "Isochoren v", "m³/mol", 1.0, 12),
    ("isobaren", "Isobaren p", "MPa", 1e6, 6),
)



class ThermoSurfaceWindow(QMainWindow):

    def __init__(self, start_stoff, surface_cfg, raster, visu_raster, berechnen, grid_generator, debug_mode=False,
                 isoline_defaults=None):
        super().__init__()

        self.base_raster = deepcopy(raster)
        self.base_visu_raster = deepcopy(visu_raster)

        # TS-VP03/04: Isolinien-Vorgaben und Ansichtsbereiche je Diagrammtyp (ueberleben das Neuerzeugen von Viewports).
        self._isoline_defaults = deepcopy(isoline_defaults) if isoline_defaults else {}
        self._isoline_prefs = {}
        self._isoline_target_id = None
        self._view_prefs = {}
        self._view_edit = {}

        # Fehlende Zusatzfelder der alten config.yaml ergänzen (Fallback: linear).
        for groups in (self.base_raster, self.base_visu_raster):
            for name, cfg in groups.items():

                if name in ("isotherm_grid", "isotherm_visu", "isochore_grid", "isochore_visu"):
                    cfg.setdefault("v_spacing", "linear")
                    cfg.setdefault("v_points", 1000)

        # Druckgrenzen der p-v-T-Zweiphasenfläche (Pa); alte config.yaml ohne Grenzen: kein Beschnitt.
        self.base_visu_raster.setdefault("two_phase_visu", {}).setdefault("p_start_twophase", 1.0e3)
        self.base_visu_raster["two_phase_visu"].setdefault("p_stop_twophase", 1.0e10)

        self.confirmed_stoff = start_stoff
        self.auto_defaults = None
        self.grid_generator = grid_generator
        self.berechnen = berechnen
        self.pvt_grid_stoff = start_stoff

        self.debug_mode = debug_mode
        self._requested_ids = []

        # Berechnungsdaten bleiben ueber Moduswechsel hinweg im Cache (keine Kameradaten im Schluessel).
        self.cache = DiagramResultCache(max_bytes=CACHE_MAX_BYTES, warn_bytes=CACHE_WARN_BYTES)
        self.manager = CalculationManager(berechnen, self.cache, self)
        self.manager.progress.connect(self._update_progress)
        self.manager.finished.connect(self._batch_finished)

        # Layer-Vorgaben je Diagrammtyp; sie ueberleben das Neuerzeugen eines Viewports.
        self._layer_prefs = {}
        self._grid_prefs = {}
        self.mode = MODE_SINGLE

        # Zustandsvergleich (Implementierung B): inaktiv = keine Zustandsauswertung, kein Plattenzugriff.
        try:
            import CoolProp
            coolprop_version = CoolProp.__version__
        except Exception:
            coolprop_version = "n/a"
        self.state_store = StateStore(StateStoreConfig(**STATE_CACHE), coolprop_version=coolprop_version)
        self.resolver = StateResolver(self.state_store)
        self.selection = SelectionManager()
        self.selection.subscribe(self._on_selection_event)
        self.comparison_view = None

        self.setWindowTitle("ThermoSurface – 3D-Zustandsdiagramme")

        # 1. Hauptfenster aufteilen
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.controls = QWidget()
        self.viewer_container = QWidget()

        splitter.addWidget(self.controls)
        splitter.addWidget(self.viewer_container)

        # Seitenleiste soll bedienbar bleiben, der 3D-Viewport flexibel wachsen.
        self.controls.setMinimumWidth(300)
        self.controls.setMaximumWidth(440)
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([350, 1050])

        self.setCentralWidget(splitter)

        # 2. Layouts erstellen
        self.controls_layout = QVBoxLayout(self.controls)
        self.controls_layout.setContentsMargins(0, 0, 0, 0)
        self.controls_layout.setSpacing(0)
        self.viewer_layout = QVBoxLayout(self.viewer_container)

        # 3. Workspace (Viewports) vor den Steuerungselementen anlegen
        self.setup_viewer()

        # 4. Steuerungselemente erstellen
        self.setup_controls(start_stoff, surface_cfg)

        # 5. Fenstergröße festlegen
        self.resize(1400, 850)


    def setup_controls(self, start_stoff, surface_cfg):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        panel = QVBoxLayout(content)
        panel.setContentsMargins(8, 8, 8, 8)
        panel.setSpacing(6)
        scroll.setWidget(content)
        self.controls_layout.addWidget(scroll)

        model_group = QGroupBox("Modellkonfiguration")
        model_form = QFormLayout(model_group)
        model_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        model_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        model_form.setContentsMargins(8, 7, 8, 7)
        model_form.setHorizontalSpacing(6)
        model_form.setVerticalSpacing(4)
        panel.addWidget(model_group)

        self.stoff_combo = QComboBox()
        self.stoff_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        for title, code in (
            ("Wasser", "Water"), 
            ("CO₂", "CO2"), 
            ("Methan", "Methane"),
            ("n-Propane", "n-Propane"), 
            ("Argon", "Argon"),
            ("Stickstoff", "Nitrogen"), 
            ("Ammoniak", "Ammonia"),
            ("Ethanol", "Ethanol"), 
            ("R134a - Kältemittel", "R134a"),
            ("n-Hexane", "n-Hexane")):

            self.stoff_combo.addItem(title, code)

        index = self.stoff_combo.findData(start_stoff)

        if index < 0:
            self.stoff_combo.addItem(start_stoff, start_stoff)
            index = self.stoff_combo.count() - 1

        self.stoff_combo.setCurrentIndex(index)

        # Darstellung: Einzel (Standard, bisherige Bedienung) oder Mehrfach (Checkboxen).
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Einzel", MODE_SINGLE)
        self.mode_combo.addItem("Mehrfach", MODE_MULTI)
        model_form.addRow("Darstellung:", self.mode_combo)

        model_form.addRow("Stoff (Auswahl):", self.stoff_combo)
        self.confirm_button = QPushButton("Stoff festlegen")
        self.reset_button = QPushButton("Automatische Werte zurücksetzen")
        model_form.addRow(self.confirm_button)
        model_form.addRow(self.reset_button)

        self.diagram_combo = QComboBox()
        for spec in DIAGRAMS:
            self.diagram_combo.addItem(spec.label, spec.id)
        model_form.addRow("Diagrammtyp:", self.diagram_combo)

        # Mehrfachauswahl: eine Checkbox je registriertem Diagramm (mindestens eines erforderlich).
        self.diagram_checks_widget = QWidget()
        checks_layout = QVBoxLayout(self.diagram_checks_widget)
        checks_layout.setContentsMargins(0, 0, 0, 0)
        checks_layout.setSpacing(2)
        self.diagram_checks = {}
        for spec in DIAGRAMS:
            box = QCheckBox(spec.label)
            box.setChecked(spec.id == "pvt")
            box.toggled.connect(self._on_selection_changed)
            self.diagram_checks[spec.id] = box
            checks_layout.addWidget(box)
        model_form.addRow("Diagramme:", self.diagram_checks_widget)
        self._model_form = model_form
        model_form.setRowVisible(self.diagram_checks_widget, False)
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)

        self.critical_info = QLabel("Startwerte aus config.yaml; noch nicht automatisch angepasst")
        self.critical_info.setWordWrap(True)
        model_form.addRow("Stoffdaten:", self.critical_info)
        self.confirm_button.clicked.connect(self.on_confirm_stoff)
        self.reset_button.clicked.connect(self.on_reset_auto)

        # Nur Parameter anzeigen, die die aktuell gewählte Darstellung steuern.
        titles = {
            "isotherm_visu":    "Darstellung: Isothermen",
            "isochore_visu":   "Darstellung: Isochoren",
            "isobare_visu":    "Darstellung: Isobaren",
            "binodale_visu":    "Darstellung: Binodale und Zweiphasenfläche",
            "spinodale_visu":   "Darstellung: Spinodale",
            "two_phase_visu":   "Darstellung: Zweiphasenauflösung",}

        self.pvt_parameter_groups = []
        self.grid_fields = {"grid_points_creation": {}, "visu_points_creation": {}}
        surface_parameters = self.base_raster["isotherm_grid"]
        surface_group = QGroupBox("Berechnung: Zustandsfläche")
        surface_form = QFormLayout(surface_group)
        surface_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        surface_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        surface_form.setContentsMargins(8, 7, 8, 7)
        surface_form.setHorizontalSpacing(4)
        surface_form.setVerticalSpacing(3)
        panel.addWidget(surface_group)
        self.pvt_parameter_groups.append(surface_group)
        surface_fields = {}
        for key in ("T_start_isotherm", "T_stop_isotherm", "T_step_isotherm"):
            control = self._make_grid_field(key, surface_parameters[key])
            control.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            label = QLabel(self._grid_label(key))
            label.setWordWrap(True)
            surface_form.addRow(label, control)
            surface_fields[key] = control
        self.grid_fields["grid_points_creation"]["isotherm_grid"] = surface_fields

        section = "visu_points_creation"
        for group_name, parameters in self.base_visu_raster.items():
            full_title = titles.get(group_name, group_name)
            display_title = ("Darstellung: Binodale / Zweiphasenfläche"
                             if group_name == "binodale_visu" else full_title)
            widget_group = QGroupBox(display_title)
            form = QFormLayout(widget_group)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setContentsMargins(8, 7, 8, 7)
            form.setHorizontalSpacing(4)
            form.setVerticalSpacing(3)
            panel.addWidget(widget_group)
            self.pvt_parameter_groups.append(widget_group)

            fields = {}
            display_parameters = dict(parameters)
            if group_name == "isotherm_visu":
                start = float(parameters["T_start_isotherm"])
                stop = float(parameters["T_stop_isotherm"])
                step = float(parameters["T_step_isotherm"])
                display_parameters.pop("T_step_isotherm")
                display_parameters["n_isotherms"] = max(
                    2, int(np.ceil((stop - start) / step)))

            for key, value in display_parameters.items():
                control = self._make_grid_field(key, value)
                control.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                label = QLabel(self._grid_label(key))
                label.setWordWrap(True)
                form.addRow(label, control)
                fields[key] = control

            if group_name == "isotherm_visu" and "n_isotherms" in fields:
                # TS-VP03: Die Isothermenwerte folgen der geometrischen Progression (Gruppe "Isolinien"); die Anzahl
                # ergibt sich aus Start, Stop und Faktor. Das Feld bleibt nur fuer die Rasterkompatibilitaet bestehen.
                fields["n_isotherms"].setEnabled(False)
                fields["n_isotherms"].setToolTip("Ersetzt durch die Isolinien-Progression (Start, Stop, Faktor).")

            self.grid_fields[section][group_name] = fields

        layer_group = QGroupBox("Darstellungsebenen")
        layer_layout = QVBoxLayout(layer_group)
        layer_layout.setContentsMargins(8, 7, 8, 7)
        layer_layout.setSpacing(3)
        self.layer_checks = {}

        for key, label in LAYER_LABELS.items():
            checkbox = QCheckBox(label)
            self.layer_checks[key] = checkbox
            layer_layout.addWidget(checkbox)
            checkbox.toggled.connect(lambda checked, k=key: self._on_layer_toggled(k, checked))

        self.export_button = QPushButton("Homogene Flächen exportieren (CSV, Spalte source)")
        self.export_button.setEnabled(False)
        layer_layout.addWidget(self.export_button)
        self.export_button.clicked.connect(self.on_export_homogeneous)

        # Checkbox für das Oberflächengitter (gilt für das aktive Diagramm)
        self.grid_checkbox = QCheckBox("Oberflächengitter anzeigen")
        self.grid_checkbox.setChecked(True)

        layer_layout.addWidget(self.grid_checkbox)

        # Signal mit Funktion verbinden
        self.grid_checkbox.toggled.connect(self.on_surface_grid_toggled)

        # Berechnungssteuerung als eigene Gruppe; derselbe Button/Callback.
        calculate_group = QGroupBox("Berechnung starten")
        calculate_layout = QVBoxLayout(calculate_group)
        calculate_layout.setContentsMargins(8, 7, 8, 7)
        self.calculate_button = QPushButton("Berechnung starten")
        calculate_layout.addWidget(self.calculate_button)

        # Sichtbarer Status und prozentualer Fortschritt unter dem Button
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p %")
        self.progress_bar.setTextVisible(True)
        calculate_layout.addWidget(self.progress_bar)

        self.progress_label = QLabel("Bereit")
        self.progress_label.setWordWrap(True)
        calculate_layout.addWidget(self.progress_label)

        # Wichtig: Parametergruppen existieren bereits. Nur die zwei
        # priorisierten Gruppen werden im Layout VOR ihnen eingefügt.
        panel.insertWidget(1, calculate_group)
        panel.insertWidget(2, layer_group)
        panel.insertWidget(3, self._build_view_group())
        panel.insertWidget(4, self._build_isoline_group())
        panel.insertWidget(5, self._build_compare_group())
        panel.addStretch()
        self.calculate_button.clicked.connect(self.on_calculate_clicked)

        self.diagram_combo.currentIndexChanged.connect(self.on_diagram_changed)
        self.on_diagram_changed()

    @staticmethod
    def _grid_label(key):
        labels = {
            "T_start_isotherm": "Temperatur von [K]",
            "T_stop_isotherm": "Temperatur bis [K]",
            "T_step_isotherm": "Temperaturabstand [K]",
            "n_isotherms": "Anzahl Isothermen",
            "T_start_isochore": "Temperatur von [K]",
            "T_stop_isochore": "Temperatur bis [K]",
            "T_start_isobare": "Temperatur von [K]",
            "T_stop_isobare": "Temperatur bis [K]",
            "T_start_sat": "Temperatur von [K]",
            "T_stop_sat": "Temperatur bis [K]",
            "T_start_spinodal": "Temperatur von [K]",
            "T_stop_spinodal": "Temperatur bis [K]",
            "v_start_isochore": "Volumen von [m³/mol]",
            "v_stop_isochore": "Volumen bis [m³/mol]",
            "v_start_isotherm": "Volumen von [m³/mol]",
            "v_stop_isotherm": "Volumen bis [m³/mol]",
            "T_step_isochore": "Temperaturabstand [K]",
            "T_step_isobare": "Temperaturabstand [K]",
            "T_step_sat": "Temperaturabstand [K]",
            "v_step_isochore": "Volumenabstand [m³/mol]",
            "v_step_isotherm": "Volumenabstand [m³/mol]",
            "p_step_isobare": "Druckabstand [Pa]",
        }
        if key in labels:
            return labels[key]
        if key == "v_spacing":
            return "Volumenraster (linear/log)"
        if key == "v_points":
            return "v-Punkte (nur log)"
        if key == "Q_points":
            return "Dampfgehalt Q: Punkte"
        if key == "p_start_twophase":
            return "Zweiphasenfläche (p-v-T): untere Druckgrenze [Pa]"
        if key == "p_stop_twophase":
            return "Zweiphasenfläche (p-v-T): obere Druckgrenze [Pa]"
        if key == "n_T":
            return "Spinodale: Temperaturpunkte"
        if key == "n_scan":
            return "Spinodale: Dichtepunkte (adaptive Suche)"
        if key == "residual_tolerance":
            return "Spinodale: interne Residualtoleranz"
        if key.startswith("v_"):
            return key + " [m³/mol]" + (" (nur linear)" if "_step_" in key else "")
        if key.startswith("T_"):
            return key + " [K]"
        if key.startswith("p_"):
            return key + " [Pa]"
        return key

    @staticmethod
    def _make_grid_field(key, value):
        if key == "v_spacing":
            field = QComboBox()
            field.addItem("Linear", "linear")
            field.addItem("Logarithmisch", "log")
            field.setCurrentIndex(field.findData(value))
            return field

        if key in ("v_points", "Q_points", "n_T", "n_scan", "n_isotherms"):
            field = QSpinBox()
            if key in ("n_T", "n_isotherms"):
                field.setRange(2, 10000)
            elif key == "n_scan":
                field.setRange(16, 10000)
            else:
                field.setRange(2, MAX_POINTS)

            field.setValue(int(value))

            return field

        field = QDoubleSpinBox()
        if key == "residual_tolerance":
            field.setDecimals(12)
            field.setRange(1e-12, 1e-2)
            field.setSingleStep(1e-8)
        elif key.startswith("v_"):
            field.setDecimals(12)
            field.setRange(1e-12, 100000.0)
            field.setSingleStep(1e-6)
        elif key.startswith("p_"):
            field.setDecimals(3)
            field.setRange(1e-3, 1e11)
            field.setSingleStep(1e5)
        else:
            field.setDecimals(6)
            field.setRange(1e-6, 1e6)
            field.setSingleStep(0.1)
        field.setValue(float(value))

        return field

    def _read_grids(self):
        raster = deepcopy(self.base_raster)
        visu_raster = deepcopy(self.base_visu_raster)
        for name, controls in self.grid_fields["grid_points_creation"].items():
            params = raster[name]
            for key, field in controls.items():
                params[key] = field.value()

        for name, controls in self.grid_fields["visu_points_creation"].items():
            params = visu_raster[name]
            for key, field in controls.items():
                params[key] = field.currentData() if key == "v_spacing" else field.value()
            if name == "isotherm_visu":
                count = int(params.pop("n_isotherms"))
                params["T_step_isotherm"] = (
                    params["T_stop_isotherm"] - params["T_start_isotherm"]
                ) / (count - 1)
                params["n_isotherms"] = count

        return raster, visu_raster

    def _write_grids(self, raster, visu_raster):
        for section, groups, source in (
                ("grid_points_creation", self.grid_fields["grid_points_creation"], raster),
                ("visu_points_creation", self.grid_fields["visu_points_creation"], visu_raster)):
            for name, controls in groups.items():
                params = source[name]
                for key, field in controls.items():
                    if key == "n_isotherms":
                        start = float(params["T_start_isotherm"])
                        stop = float(params["T_stop_isotherm"])
                        step = float(params["T_step_isotherm"])
                        value = max(2, int(np.ceil((stop - start) / step)))
                    else:
                        value = params[key]
                    if key == "v_spacing":
                        field.setCurrentIndex(field.findData(value))
                    else:
                        field.setValue(value)

    
    def selected_diagram_ids(self):
        """Ausgewaehlte Diagramme in Registry-Reihenfolge (Einzel: genau eines, Mehrfach: Checkboxen)."""
        if self.mode == MODE_SINGLE:
            return [self.diagram_combo.currentData()]
        return [spec.id for spec in DIAGRAMS if self.diagram_checks[spec.id].isChecked()]

    def on_confirm_stoff(self):

        stoff = self.stoff_combo.currentData()
        ids = self.selected_diagram_ids()

        # Nur p-v-T benoetigt die automatische Rasteranpassung; alle anderen Diagramme
        # erstellen ihr eigenes Raster bei Berechnungsstart.
        if "pvt" not in ids:
            self.confirmed_stoff = stoff
            self.selection.clear_for_fluid_change(stoff)
            label = DIAGRAMS.get(ids[0]).confirm_label if len(ids) == 1 else "Mehrfachansicht"
            self.critical_info.setText(f"{stoff}: Stoff übernommen. " f"{label}-Raster wird beim Berechnungsstart erzeugt.")
            self.statusBar().showMessage(f"{stoff} übernommen; " f"{label}-Berechnung noch nicht gestartet.")

            return

        # p-v-T:
        # Bisherige automatische Rasteranpassung beibehalten.
        try:
            raster, visu, meta = self.grid_generator(stoff, self.base_raster, self.base_visu_raster)

        except Exception as exc:
            if self.debug_mode:
                LOGGER.exception("Automatische Stoffanpassung fehlgeschlagen")
            QMessageBox.warning(self, "Stoff konnte nicht übernommen werden", str(exc))

            return

        self.auto_defaults = deepcopy((raster, visu, meta))
        self.base_raster = deepcopy(raster)
        self.base_visu_raster = deepcopy(visu)
        self._write_grids(raster, visu)

        self.confirmed_stoff = stoff
        self.pvt_grid_stoff = stoff
        self.selection.clear_for_fluid_change(stoff)
        self.critical_info.setText(f"{stoff}: Tc={meta['Tc']:.3f} K, " f"pc={meta['pc']/1e6:.3f} MPa, " f"vc={meta['vc']:.8g} m³/mol, " f"Ttr={meta['Ttr']:.3f} K")

        self.statusBar().showMessage(f"Raster für {stoff} angepasst; ""Berechnung noch nicht gestartet.")

    def on_reset_auto(self):
        if "pvt" not in self.selected_diagram_ids():
            return
        
        if self.auto_defaults is None:
            # Beim ersten Zurücksetzen die aktuell bestätigte Stoffauswahl verwenden.
            current_index = self.stoff_combo.currentIndex()
            confirmed_index = self.stoff_combo.findData(self.confirmed_stoff)
            self.stoff_combo.setCurrentIndex(confirmed_index)
            self.on_confirm_stoff()
            self.stoff_combo.setCurrentIndex(current_index)
            return
        
        raster, visu, _meta = self.auto_defaults
        self.base_raster = deepcopy(raster)
        self.base_visu_raster = deepcopy(visu)
        self._write_grids(raster, visu)
        self.statusBar().showMessage("Automatische Standardwerte wiederhergestellt.")

    def get_settings(self):
        raster, visu_raster = self._read_grids()
        ids = self.selected_diagram_ids()
        viewport = self.workspace.active
        layers = viewport.effective_layers() if viewport is not None else {}
        return {
            "stoff": self.confirmed_stoff,
            "diagram_type": ids[0] if ids else None,
            "diagram_types": ids,
            "raster": raster,
            "visu_raster": visu_raster,
            "layers": layers,}

    def on_calculate_clicked(self):

        # Keine parallelen Berechnungen
        if self.manager.is_running:
            return

        # Mehrfachansicht braucht mindestens ein angekreuztes Diagramm
        if not self.selected_diagram_ids():
            QMessageBox.warning(self, "Kein Diagramm ausgewählt", "Bitte mindestens ein Diagramm ankreuzen.")
            self.statusBar().showMessage("Berechnung abgewiesen: kein Diagramm ausgewählt.")
            return

        # Stoff muss zuerst bestätigt werden
        if self.stoff_combo.currentData() != self.confirmed_stoff:

            QMessageBox.warning(self, "Stoffauswahl nicht bestätigt", "Bitte zuerst 'Stoff festlegen' wählen.")
            return

        settings = self.get_settings()
        ids = settings["diagram_types"]

        # Verhindert die Verwendung eines p-v-T-Rasters,
        # das noch zu einem zuvor bestätigten Stoff gehört.
        if ("pvt" in ids and self.pvt_grid_stoff != self.confirmed_stoff):
            QMessageBox.warning(self, "p-v-T-Raster aktualisieren","Bitte zuerst 'Stoff festlegen' wählen, ""damit die p-v-T-Raster angepasst werden.")
            return

        stoff, raster, visu_raster = self.prepare_calculation(settings)

        isolines, isoline_error = self._validate_isolines(ids)
        if isoline_error:
            QMessageBox.warning(self, "Ungültige Isolinienvorgabe", isoline_error)
            self.statusBar().showMessage(f"Berechnung abgewiesen: {isoline_error}")
            return

        # Auswahl der gestarteten Berechnung festhalten; sie darf waehrend des QThread-Laufs nicht wechseln.
        self._requested_ids = list(ids)
        tasks = [DiagramTask(diagram_id, stoff, deepcopy(raster), deepcopy(visu_raster),
                             isolines=deepcopy(isolines[diagram_id]) if diagram_id in self._isoline_prefs else None)
                 for diagram_id in ids]
        self._set_busy(True)
        self.progress_bar.setValue(0)
        self.progress_label.setText("Berechnung wird vorbereitet ...")
        self.statusBar().showMessage(f"Berechne {', '.join(ids)} für {stoff} ...")
        if not self.manager.start(tasks):
            self._set_busy(False)

    def _set_busy(self, busy):
        for widget in (self.calculate_button, self.confirm_button, self.stoff_combo,
                       self.diagram_combo, self.mode_combo, self.diagram_checks_widget):
            widget.setEnabled(not busy)
        self.reset_button.setEnabled(not busy and "pvt" in self.selected_diagram_ids())

    @Slot(int, str)
    def _update_progress(self, percent, phase):
        self.progress_bar.setValue(int(percent))
        self.progress_label.setText(phase)
        self.statusBar().showMessage(phase)

    # ------------------------------------------------------------------ Ergebnisverarbeitung
    @Slot(object)
    def _batch_finished(self, batch):
        """Alle Diagramme der Warteschlange sind fertig: Ansicht aufbauen, Fehler je Diagramm melden."""
        self._set_busy(False)
        self.progress_bar.setValue(96)
        self.progress_label.setText("Erstelle 3D-Visualisierung ...")
        self.statusBar().showMessage("Erstelle 3D-Visualisierung ...")

        errors = [(o.task.label, o.error) for o in batch.failed]
        viewports = self._display_outcomes(batch.succeeded, errors)

        status_parts = []
        info_failed = {label for label, _ in errors}
        for outcome in batch.succeeded:
            if outcome.task.label in info_failed or outcome.task.diagram_id not in viewports:
                continue
            status = f"{outcome.task.diagram_id} abgeschlossen: {self.confirmed_stoff}"
            if outcome.from_cache:
                status += " (aus Cache)"
            report = outcome.daten.get("isoline_report") if isinstance(outcome.daten, dict) else None
            if report:
                status += " | Isolinien T/v/p: " + "/".join(str(report[f]["n_levels"]) for f in ISOLINE_FAMILIES)
            if outcome.task.diagram_id == "pvt":
                status, tooltips, progress_text = self._pvt_report(outcome.daten, status)
                viewports["pvt"].tooltips = tooltips
                if progress_text:
                    self.progress_label.setText(progress_text)
            status_parts.append(status)

        if self.cache.warnings:
            status_parts.append(self.cache.warnings[-1])
            self.cache.warnings.clear()
        if self.cache.evictions:
            status_parts.append(f"{len(self.cache.evictions)} ältere Ergebnis(se) aus dem Cache verdrängt (werden bei Bedarf neu berechnet)")
            self.cache.evictions.clear()

        if errors:
            self.progress_label.setText("Teilweise fehlgeschlagen: " + ", ".join(label for label, _ in errors))
            self._report_errors(errors, shown=len(viewports))
        elif viewports:
            self.progress_bar.setValue(100)
            if not self.progress_label.text().startswith("Druckachse"):
                self.progress_label.setText("Berechnung und Darstellung abgeschlossen")
        self._refresh_layer_panel()
        self._refresh_view_panel()
        self._refresh_isoline_panel()
        self._sync_markers()
        self.on_diagram_changed(preserve_status=True)
        if status_parts:
            self.statusBar().showMessage(" | ".join(status_parts))
        elif errors:
            self.statusBar().showMessage("Fehler")

    def _report_errors(self, errors, shown):
        lines = "\n".join(f"• {label}: {message}" for label, message in errors)
        if self.mode == MODE_SINGLE or shown == 0:
            QMessageBox.critical(self, "Berechnung fehlgeschlagen", lines)
        else:
            QMessageBox.warning(self, "Diagramme übersprungen",
                                "Folgende Diagramme konnten nicht erstellt werden und wurden übersprungen; "
                                "alle anderen sind verfügbar:\n" + lines)

    def _display_outcomes(self, outcomes, errors):
        """Zeigt erfolgreiche Ergebnisse im Workspace; Renderfehler werden wie Rechenfehler isoliert."""
        if not outcomes:
            return {}

        # Layer-/Gittereinstellungen aller bestehenden Viewports sichern (je Diagrammtyp).
        self._remember_viewport_state()
        viewports, created = self.workspace.ensure([o.task.diagram_id for o in outcomes])
        for diagram_id in created:
            viewport = viewports[diagram_id]
            viewport.set_layer_state(self._layer_prefs.get(diagram_id, {}))
            viewport.grid_on = self._grid_prefs.get(diagram_id, True)
            viewport.tooltips = {}
            # Neu gebildete Viewports desselben Typs uebernehmen den gespeicherten Ansichtsbereich.
            if diagram_id in self._view_prefs:
                viewport.view_state = self._view_prefs[diagram_id].copy()

        for outcome in outcomes:
            diagram_id = outcome.task.diagram_id
            viewport = viewports[diagram_id]
            if viewport.has_result and viewport.result_key == outcome.key:
                continue  # unveraendertes Ergebnis: Kamera, Layer und Renderzustand bleiben erhalten
            try:
                viewport.show_result(outcome.daten, outcome.key)
                self._after_render(viewport, outcome)
            except Exception as exc:
                LOGGER.exception("Visualisierung %s fehlgeschlagen", diagram_id)
                errors.append((outcome.task.label, f"Darstellung fehlgeschlagen: {exc}"))
                self.workspace.ensure([i for i in viewports if i != diagram_id])
                viewports = dict(self.workspace.viewports)
        return viewports

    def _after_render(self, viewport, outcome):
        viewport.info_text = viewport.spec.info_text(outcome.daten, self.confirmed_stoff)
        if self.debug_mode and viewport.diagram_id == "pvt":
            from Debug import diagnose_visualisierung
            from visualization import to_xyz

            daten = outcome.daten
            diagnose_visualisierung(
                daten, viewport.actors,
                lambda points, critical: to_xyz(points, critical, daten.get("pressure_axis", "log")))

    def _remember_viewport_state(self):
        for viewport in self.workspace.ordered():
            self._layer_prefs[viewport.diagram_id] = dict(viewport.layer_state)
            self._grid_prefs[viewport.diagram_id] = viewport.grid_on
            self._view_prefs[viewport.diagram_id] = viewport.view_state.copy()

    def _pvt_report(self, daten, status):
        """AP3-Statusmeldungen und Tooltips fuer p-v-T; Rueckgabe (status, tooltips, progress_text)."""
        tooltips = {}
        progress_text = None
        if isinstance(daten, dict):
            # AP3: Unvollstaendige oder nicht gepruefte Spinodalen nicht als
            # uneingeschraenkt validiert in der GUI praesentieren.
            checks = daten.get("spinodal_validation", {})
            incomplete = [branch for branch in ("liquid", "vapor")
                          if not checks.get(branch, {}).get("complete", False)]
            message = ("" if incomplete else
                       "AP3: beide Spinodalaeste innerhalb der Kriterien validiert")
            solver = daten.get("spinodal_solver", {})
            if solver:
                details = solver.get("temperature", {})
                message += (f"{' | ' if message else ''}"
                            f"Dampfquelle: {solver.get('selected', 'legacy')}, "
                            f"neue HEOS-Punkte: {details.get('valid_points', 0)}/"
                            f"{details.get('requested_points', 0)}, "
                            f"Abdeckung: {details.get('success_fraction', 0):.1%}")
                if solver.get("fallback_reason"):
                    message += " | " + solver["fallback_reason"]
            tooltips["spinodale"] = message
            # Neue homogene Bereiche haben eine unabhaengige Verfuegbarkeitsdiagnose.
            homogeneous = daten.get("homogeneous_validation", {})
            reports = homogeneous.get("surfaces", {})
            for key in ("metastable_liquid_surface", "metastable_vapor_surface", "unstable_surface"):
                info = reports.get(key, {})
                valid_rows = int(info.get("valid_rows", 0))
                partial_rows = int(info.get("partial_rows", 0))
                missing_rows = len(info.get("missing_rows", []))
                triangles = int(info.get("candidate_triangles", 0))
                cov = info.get("coverage") or {}
                cov_value = cov.get("coverage")
                gaps = len(info.get("gap_rows", {}))
                extra = ""
                if cov_value is not None:
                    extra += f" Zellabdeckung (auswertbar): {cov_value:.1%}."
                if gaps:
                    extra += (f" {gaps} T-Zeilen mit EOS-Artefakt-Luecke (zusaetzliche Extrema "
                              f"zwischen den Spinodalen) - dort nur spinodalnahe Aeste.")
                tooltips[key] = (
                    f"Homogene EOS: {valid_rows} vollstaendige, "
                    f"{partial_rows} teilweise gueltige, {missing_rows} fehlende T-Zeilen. "
                    f"{triangles} potentiell sichtbare Dreiecke. "
                    f"p-Achse: {daten.get('pressure_axis', 'log')}." + extra)
            if homogeneous.get("error"):
                status += " | Homogene Flaechen: " + homogeneous["error"]
            elif homogeneous.get("enabled") and not homogeneous.get("available"):
                status += " | Homogene Flaechen: keine renderbaren Dreiecke"
            elif homogeneous.get("enabled"):
                partial = [k for k in ("metastable_liquid_surface", "metastable_vapor_surface")
                           if reports.get(k, {}).get("missing_rows")]
                if partial:
                    status += " | Homogene HEOS-Flaechen unvollstaendig: " + ", ".join(partial)
            replacement = daten.get("replacement_status", {})
            sub_info = replacement.get("rows", {})
            tooltips["replacement_surface"] = (
                "MODELL-/ERSATZFLAECHE, keine EOS-Auswertung: kubische Hermite-Verbindung "
                "p(s)=p_L+(p_V-p_L)(3s^2-2s^3) zwischen den HEOS-Spinodalpunkten jeder Isotherme "
                "(Randpunkte = unveraenderte HEOS-Werte). Keine Fundamentalgleichung, keine abgeleiteten "
                f"Groessen. Zeilen: {sub_info}")
            if replacement.get("notice"):
                status += " | " + replacement["notice"]
            if daten.get("pressure_axis_notice"):
                status += " | " + daten["pressure_axis_notice"]
                progress_text = "Druckachse automatisch auf 'symlog' umgestellt (p <= 0)"
        return status, tooltips, progress_text

    # ------------------------------------------------------------------ Viewer / Lebenszyklus
    def setup_viewer(self):
        self.viewer_layout.setContentsMargins(0, 0, 0, 0)
        self.workspace = DiagramWorkspace(self.viewer_container)
        self.workspace.set_single_mode(True)
        self.workspace.active_changed.connect(self._on_active_changed)
        self.workspace.status_message.connect(lambda text: self.statusBar().showMessage(text))
        self.workspace.point_clicked.connect(self._on_point_clicked)
        self.viewer_layout.addWidget(self.workspace)

    def closeEvent(self, event):
        if self.manager.is_running:
            event.ignore()
            QMessageBox.information(self, "Berechnung läuft", "Bitte auf den Abschluss der Berechnung warten, bevor das Fenster geschlossen wird.")

            return

        self.manager.shutdown()
        if self.comparison_view is not None:
            self.comparison_view.dispose()
            self.comparison_view = None
        self.state_store.flush()   # nur wirksam, wenn der Datei-Cache aktiviert ist
        self.workspace.close_all()
        super().closeEvent(event)

    # ------------------------------------------------------------------ Modus / Auswahl
    def on_mode_changed(self, _index=None):
        """Einzel <-> Mehrfach: Ansicht zuruecksetzen, Berechnungsdaten bleiben im Cache."""
        self.mode = self.mode_combo.currentData()
        single = self.mode == MODE_SINGLE
        self._model_form.setRowVisible(self.diagram_combo, single)
        self._model_form.setRowVisible(self.diagram_checks_widget, not single)
        self._remember_viewport_state()
        self.workspace.clear()
        self.workspace.set_single_mode(single)
        self.on_diagram_changed()
        self._refresh_layer_panel()
        self._refresh_view_panel()
        self._refresh_isoline_panel()

    def _on_selection_changed(self, _checked=None):
        self.on_diagram_changed()
        if self.workspace.active is None:
            self._refresh_layer_panel()
        self._refresh_isoline_panel()

    # ------------------------------------------------------------------ Layer-Panel (aktives Diagramm)
    def _on_active_changed(self, _viewport):
        self._refresh_layer_panel()
        self._refresh_view_panel()
        self._refresh_isoline_panel()

    def _refresh_layer_panel(self):
        viewport = self.workspace.active
        if viewport is not None:
            spec = viewport.spec
            state = viewport.layer_state
            available = viewport.available_layers()
            tooltips = getattr(viewport, "tooltips", {})
            self.critical_info.setText(getattr(viewport, "info_text", "") or self.critical_info.text())
        else:
            ids = self.selected_diagram_ids()
            spec = DIAGRAMS.get(ids[0]) if ids else None
            state = spec.initial_layer_state() if spec else {}
            available = {}
            tooltips = {}

        for key, checkbox in self.layer_checks.items():
            usable = bool(available.get(key, False))
            checkbox.blockSignals(True)
            checkbox.setVisible(spec is not None and key in spec.layers)
            checkbox.setEnabled(usable)
            checkbox.setChecked(bool(state.get(key, False)) and (usable or viewport is None))
            checkbox.setToolTip(tooltips.get(key, ""))
            checkbox.blockSignals(False)

        self.grid_checkbox.blockSignals(True)
        self.grid_checkbox.setChecked(viewport.grid_on if viewport is not None else True)
        self.grid_checkbox.setEnabled(viewport is not None)
        self.grid_checkbox.blockSignals(False)
        self.export_button.setEnabled(viewport is not None and viewport.diagram_id == "pvt" and viewport.has_result)

    def _on_layer_toggled(self, key, checked):
        viewport = self.workspace.active
        if viewport is None:
            return

        # HEOS-Rohfortsetzung und Ersatzflaeche nie gleichzeitig: zuletzt eingeschaltete gewinnt.
        if key in HOMOGENEOUS_CHOICE:
            states = {k: self.layer_checks[k].isChecked() for k in HOMOGENEOUS_CHOICE}
            for other, value in resolve_homogeneous_choice(states, key).items():
                box = self.layer_checks[other]
                if box.isChecked() != value:
                    box.blockSignals(True)
                    box.setChecked(value)
                    box.blockSignals(False)

        available = viewport.available_layers()
        viewport.set_layer_state({k: box.isChecked() for k, box in self.layer_checks.items() if available.get(k)})
        viewport.apply_layers()

    # ------------------------------------------------------------------ Ansichtsbereich (TS-VP04)
    def _build_view_group(self):
        group = QGroupBox("Ansichtsbereich (nur Darstellung)")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(4)
        self.view_group = group

        self.view_check = QCheckBox("Ansichtsbereich manuell begrenzen")
        self.view_check.setToolTip("Begrenzt nur die gerenderte Darstellung des aktiven Diagramms; es wird nichts neu "
                                   "berechnet. Wirksam erst nach 'Ansicht anwenden'.")
        layout.addWidget(self.view_check)

        form = QFormLayout()
        form.setHorizontalSpacing(4)
        form.setVerticalSpacing(3)
        self.view_fields = []
        self.view_labels = []
        for index in range(6):
            edit = QLineEdit()
            edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            label = QLabel("")
            label.setWordWrap(True)
            form.addRow(label, edit)
            self.view_fields.append(edit)
            self.view_labels.append(label)
        layout.addLayout(form)

        row = QHBoxLayout()
        self.view_apply_button = QPushButton("Ansicht anwenden")
        self.view_reset_button = QPushButton("Zurücksetzen")
        row.addWidget(self.view_apply_button)
        row.addWidget(self.view_reset_button)
        layout.addLayout(row)
        self.view_camera_button = QPushButton("Standardkamera")
        self.view_camera_button.setToolTip("Stellt die Standardkamera des Renderers wieder her (Ansichtsbereich bleibt).")
        layout.addWidget(self.view_camera_button)
        self.view_message = QLabel("")
        self.view_message.setWordWrap(True)
        self.view_message.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.view_message)

        self.view_check.toggled.connect(self.on_view_check_toggled)
        self.view_apply_button.clicked.connect(self.on_view_apply)
        self.view_reset_button.clicked.connect(self.on_view_reset)
        self.view_camera_button.clicked.connect(self.on_view_default_camera)
        self._refresh_view_panel()
        return group

    @staticmethod
    def _format_field(value):
        return f"{value:.6g}"

    def _refresh_view_panel(self):
        """Beschriftungen, Werte und Aktivierung aus dem aktiven Viewport (ein Zustand je Diagramm)."""
        viewport = self.workspace.active if hasattr(self, "workspace") else None
        usable = viewport is not None and viewport.has_result
        diagram_id = viewport.diagram_id if viewport is not None else None
        specs = diagram_axes.axes_for(diagram_id) if diagram_id else None
        for index, (label, edit) in enumerate(zip(self.view_labels, self.view_fields)):
            if specs is None:
                label.setText("–")
            else:
                spec = specs[index // 2]
                letter = diagram_axes.AXIS_LETTERS[index // 2]
                label.setText(f"{letter}: {spec.key} {'von' if index % 2 == 0 else 'bis'} [{spec.unit}]")

        state = viewport.view_state if viewport is not None else None
        editing = bool(usable and (state.enabled or self._view_edit.get(diagram_id, False)))
        self.view_check.blockSignals(True)
        self.view_check.setChecked(editing)
        self.view_check.blockSignals(False)
        self.view_check.setEnabled(usable)
        texts = None
        if usable:
            texts = state.fields or self._suggest_view_fields(viewport)
        for index, edit in enumerate(self.view_fields):
            edit.setText(texts[index] if texts else "")
            edit.setEnabled(editing)
        self.view_apply_button.setEnabled(editing)
        self.view_reset_button.setEnabled(usable and (state.enabled or state.user_defined))
        self.view_camera_button.setEnabled(usable)
        self.view_group.setEnabled(True)
        self.view_message.setStyleSheet("")
        if not usable:
            self.view_message.setText("Zuerst eine Berechnung starten, dann kann der Ansichtsbereich begrenzt werden.")
        elif state.enabled:
            self.view_message.setText("Manuelle Begrenzung aktiv (nur Darstellung, keine Neuberechnung).")
        else:
            self.view_message.setText("")

    def _suggest_view_fields(self, viewport):
        """Vorschlag: aktuelle automatische Grenzen in physikalischen Anzeigeeinheiten (noch nicht angewendet)."""
        from axes_view import auto_bounds
        box = auto_bounds(viewport.diagram_id, viewport.actors)
        if box is None:
            return None
        try:
            bounds = diagram_axes.display_bounds_to_physical(viewport.diagram_id, viewport.daten, box)
        except Exception:
            return None
        return [self._format_field(x) for pair in bounds for x in pair]

    def _store_view_state(self, viewport):
        self._view_prefs[viewport.diagram_id] = viewport.view_state.copy()

    def on_view_check_toggled(self, checked):
        viewport = self.workspace.active
        if viewport is None or not viewport.has_result:
            return
        self._view_edit[viewport.diagram_id] = bool(checked)
        if not checked and viewport.view_state.enabled:
            viewport.reset_manual_view()
            self._store_view_state(viewport)
        self._refresh_view_panel()

    def on_view_apply(self):
        viewport = self.workspace.active
        if viewport is None or not viewport.has_result:
            return
        try:
            stats = viewport.apply_manual_view([edit.text() for edit in self.view_fields])
        except ViewportBoundsError as exc:
            # Der zuletzt gueltige Ausschnitt bleibt erhalten; nichts wird veraendert.
            self.view_message.setStyleSheet("color: #b00020;")
            self.view_message.setText(str(exc))
            self.statusBar().showMessage(f"Ansichtsbereich nicht angewendet: {exc}")
            return
        self._view_edit[viewport.diagram_id] = True
        self._store_view_state(viewport)
        self._refresh_view_panel()
        text = "Ansichtsbereich angewendet (nur Darstellung, keine Neuberechnung)."
        if stats is not None and stats["empty"] == stats["actors"]:
            text += " Der Ausschnitt enthält keine Geometrie; Rahmen und Achsen bleiben sichtbar."
        self.view_message.setText(text)
        self.statusBar().showMessage(text)

    def on_view_reset(self):
        viewport = self.workspace.active
        if viewport is None or not viewport.has_result:
            return
        viewport.reset_manual_view()
        self._view_edit[viewport.diagram_id] = False
        self._store_view_state(viewport)
        self._refresh_view_panel()
        self.view_message.setText("Zurückgesetzt: vollständige Originaldarstellung mit automatischen Grenzen.")
        self.statusBar().showMessage("Ansichtsbereich zurückgesetzt.")

    def on_view_default_camera(self):
        viewport = self.workspace.active
        if viewport is not None and viewport.has_result:
            viewport.restore_default_camera()

    # ------------------------------------------------------------------ Isolinien (TS-VP03)
    def _build_isoline_group(self):
        group = QGroupBox("Isolinien (geometrische Progression)")
        form = QFormLayout(group)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setContentsMargins(8, 7, 8, 7)
        form.setHorizontalSpacing(4)
        form.setVerticalSpacing(3)
        self.isoline_group = group
        self.isoline_fields = {}

        note = QLabel("x[n+1] = f · x[n], x[0] = Start, solange x ≤ Stop. 0 = automatisch (Diagrammbereich). "
                      "Beeinflusst nur die Isolinienwerte, nicht die Auflösung der Flächen oder Linien.")
        note.setWordWrap(True)
        note.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        form.addRow(note)
        self.isoline_diagram_label = QLabel("")
        self.isoline_diagram_label.setWordWrap(True)
        self.isoline_diagram_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        form.addRow(self.isoline_diagram_label)

        for family, title, unit, _to_si, decimals in ISOLINE_GUI_FAMILIES:
            fields = {}
            for key, text in (("start", f"{title}: Start [{unit}]"), ("stop", f"{title}: Stop [{unit}]"),
                              ("factor", f"{title}: Faktor f")):
                spin = QDoubleSpinBox()
                spin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                if key == "factor":
                    spin.setDecimals(4)
                    spin.setRange(1.0001, 1000.0)
                    spin.setSingleStep(0.01)
                    spin.setValue(FACTOR_DEFAULT)
                else:
                    spin.setDecimals(decimals)
                    spin.setRange(0.0, 1e12)
                    spin.setSpecialValueText("auto")
                    spin.setSingleStep({"isothermen": 10.0, "isochoren": 1e-5, "isobaren": 1.0}[family])
                spin.valueChanged.connect(self._on_isoline_field_changed)
                label = QLabel(text)
                label.setWordWrap(True)
                form.addRow(label, spin)
                fields[key] = spin
            self.isoline_fields[family] = fields

        self.isoline_report_label = QLabel("")
        self.isoline_report_label.setWordWrap(True)
        self.isoline_report_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        form.addRow(self.isoline_report_label)
        self._refresh_isoline_panel()
        return group

    def _isoline_target(self):
        ids = self.selected_diagram_ids() if hasattr(self, "diagram_checks") else []
        if self.mode == MODE_SINGLE:
            return ids[0] if ids else None
        active = self.workspace.active if hasattr(self, "workspace") else None
        if active is not None and active.diagram_id in ids:
            return active.diagram_id
        return ids[0] if ids else None

    def _isoline_gui_values(self, diagram_id):
        """GUI-Werte (Einheiten der Felder; 0 = auto) eines Diagramms: Benutzereingabe vor config.yaml-Vorgabe."""
        if diagram_id in self._isoline_prefs:
            return self._isoline_prefs[diagram_id]
        try:
            raw = normalise_isoline_config(self._isoline_defaults.get(diagram_id))
        except IsolineLevelError:
            raw = normalise_isoline_config(None)
        values = {}
        for family, _title, _unit, to_si, _decimals in ISOLINE_GUI_FAMILIES:
            cfg = raw[family]
            values[family] = {"start": 0.0 if cfg["start"] is None else cfg["start"] / to_si,
                              "stop": 0.0 if cfg["stop"] is None else cfg["stop"] / to_si,
                              "factor": cfg["factor"]}
        return values

    def _refresh_isoline_panel(self):
        target = self._isoline_target()
        self._isoline_target_id = target
        usable = target is not None
        self.isoline_group.setEnabled(usable)
        self.isoline_diagram_label.setText(
            f"Gilt für: {DIAGRAMS.get(target).label}" if usable else "Kein Diagramm ausgewählt.")
        values = self._isoline_gui_values(target) if usable else None
        for family, fields in self.isoline_fields.items():
            for key, spin in fields.items():
                spin.blockSignals(True)
                spin.setValue(values[family][key] if values else (FACTOR_DEFAULT if key == "factor" else 0.0))
                spin.blockSignals(False)
        report = None
        viewport = self.workspace.viewports.get(target) if usable and hasattr(self, "workspace") else None
        if viewport is not None and isinstance(viewport.daten, dict):
            report = viewport.daten.get("isoline_report")
        self.isoline_report_label.setText(self._isoline_report_text(report))

    @staticmethod
    def _isoline_report_text(report):
        if not report:
            return ""
        parts = []
        for family, title, unit, _to_si, _d in ISOLINE_GUI_FAMILIES:
            entry = report.get(family)
            if entry is None:
                continue
            text = f"{title}: {entry['n_levels']} Level(s)"
            if entry.get("dropped_outside_domain"):
                text += f", {len(entry['dropped_outside_domain'])} außerhalb des Bereichs verworfen"
            if entry.get("failed_levels") or entry.get("empty_levels"):
                text += f", {len(entry.get('failed_levels', [])) + len(entry.get('empty_levels', []))} ohne Kurve"
            parts.append(text)
        return "Letzte Berechnung – " + "; ".join(parts)

    def _on_isoline_field_changed(self, _value=None):
        target = self._isoline_target_id
        if target is None:
            return
        self._isoline_prefs[target] = {
            family: {key: float(spin.value()) for key, spin in fields.items()}
            for family, fields in self.isoline_fields.items()}

    def isoline_config_si(self, diagram_id):
        """Isolinienvorgabe eines Diagramms in SI (T [K], v [m³/mol], p [Pa]); start/stop None = automatisch."""
        values = self._isoline_gui_values(diagram_id)
        out = {}
        for family, _title, _unit, to_si, _decimals in ISOLINE_GUI_FAMILIES:
            gui = values[family]
            out[family] = {"start": gui["start"] * to_si if gui["start"] > 0 else None,
                           "stop": gui["stop"] * to_si if gui["stop"] > 0 else None,
                           "factor": float(gui["factor"])}
        return out

    def _validate_isolines(self, ids):
        """Prueft die Isolinienvorgaben aller zu berechnenden Diagramme; liefert (konfigurationen, fehlermeldung)."""
        configs = {}
        for diagram_id in ids:
            cfg = self.isoline_config_si(diagram_id)
            try:
                normalised = normalise_isoline_config(cfg)
                for family in ISOLINE_FAMILIES:
                    entry = normalised[family]
                    if entry["start"] is not None and entry["stop"] is not None:
                        generate_levels(entry["start"], entry["stop"], entry["factor"], entry["max_levels"])
                    elif entry["factor"] <= 1.0:
                        raise IsolineLevelError("factor muss > 1 sein.")
            except IsolineLevelError as exc:
                return None, f"{DIAGRAMS.get(diagram_id).label}: {exc}"
            configs[diagram_id] = cfg
        return configs, None

    # ------------------------------------------------------------------ Zustandsvergleich (Implementierung B)
    def _build_compare_group(self):
        group = QGroupBox("Zustandsvergleich")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(4)

        self.compare_check = QCheckBox("Vergleichsmodus aktiv (Einfachklick auf eine Fläche wählt einen Punkt)")
        self.compare_check.setToolTip("Inaktiv: keine Zustandsauswertung, keine Plattenzugriffe des Zusatzcaches.")
        self.compare_check.toggled.connect(self.on_compare_toggled)
        layout.addWidget(self.compare_check)

        self.compare_list = QListWidget()
        self.compare_list.setMinimumHeight(110)
        self.compare_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.compare_list)

        row = QHBoxLayout()
        self.compare_delete_button = QPushButton("Löschen")
        self.compare_reset_button = QPushButton("Alle zurücksetzen")
        self.compare_up_button = QPushButton("▲")
        self.compare_down_button = QPushButton("▼")
        for button in (self.compare_delete_button, self.compare_reset_button, self.compare_up_button,
                       self.compare_down_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.compare_open_button = QPushButton("Vergleichsdiagramm öffnen …")
        layout.addWidget(self.compare_open_button)
        self.compare_info = QLabel("Keine Punkte ausgewählt.")
        self.compare_info.setWordWrap(True)
        layout.addWidget(self.compare_info)

        self.compare_delete_button.clicked.connect(self.on_compare_delete)
        self.compare_reset_button.clicked.connect(self.on_compare_reset)
        self.compare_up_button.clicked.connect(lambda: self.on_compare_move(-1))
        self.compare_down_button.clicked.connect(lambda: self.on_compare_move(+1))
        self.compare_open_button.clicked.connect(self.on_open_comparison)
        self._refresh_compare_list()
        return group

    def on_compare_toggled(self, checked):
        self.workspace.picking_enabled = checked
        self.statusBar().showMessage(
            "Vergleichsmodus aktiv: Einfachklick wählt einen Punkt, Doppelklick maximiert, Ziehen rotiert."
            if checked else "Vergleichsmodus beendet (gewählte Punkte bleiben in dieser Sitzung erhalten).")

    @Slot(object, int, int)
    def _on_point_clicked(self, viewport, x, y):
        if not self.compare_check.isChecked() or not viewport.has_result:
            return
        key = viewport.result_key
        fluid = key.fluid if key is not None else self.confirmed_stoff
        try:
            index = self.state_store.ensure(fluid, viewport.diagram_id, viewport.daten, key)
            pick = pick_from_viewport(viewport, index, x, y)
            state = self.resolver.resolve(pick, index)
            original = {f"{axis} [{SOURCE_AXIS_UNITS[axis]}]": value
                        for axis, value in zip(index.adapter.axes, pick.physical_hint["native_interpolated"])}
            point = self.selection.add(state, pick.display_xyz, original)
        except PickError as exc:
            self.statusBar().showMessage(f"Kein Punkt gewählt: {exc}")
            return
        except FluidMismatch as exc:
            QMessageBox.warning(self, "Anderes Fluid", str(exc))
            return
        except NotAddable as exc:
            QMessageBox.warning(self, "Zustand nicht darstellbar", str(exc))
            self.statusBar().showMessage(str(exc))
            return
        note = f" – {state.notes[0]}" if state.notes else ""
        self.statusBar().showMessage(f"Punkt {point.selection_index} ({point.source_diagram}): "
                                     f"{state.phase} [{state.validity}]{note}")

    def _on_selection_event(self, _event):
        self._refresh_compare_list()
        self._sync_markers()

    def _refresh_compare_list(self):
        current = self._selected_compare_index()
        self.compare_list.clear()
        for point in self.selection.points:
            state = point.state
            item = QListWidgetItem(f"{point.selection_index}  {point.source_diagram}  {state.phase}  [{state.validity}]")
            item.setData(Qt.ItemDataRole.UserRole, point.selection_index)
            item.setForeground(Qt.GlobalColor.black)
            item.setToolTip(point.description() + "\n" + "\n".join(state.notes))
            self.compare_list.addItem(item)
            if point.selection_index == current:
                self.compare_list.setCurrentItem(item)
        n = len(self.selection)
        self.compare_info.setText("Keine Punkte ausgewählt." if not n else
                                  f"{n} Punkt(e), Fluid: {self.selection.fluid}. Nummern bleiben nach dem Löschen eindeutig.")

    def _selected_compare_index(self):
        item = self.compare_list.currentItem() if hasattr(self, "compare_list") else None
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _sync_markers(self):
        for viewport in self.workspace.ordered():
            viewport.set_selection_markers(
                [(p.selection_index, p.display_xyz, p.color) for p in self.selection.points_of(viewport.diagram_id)])

    def on_compare_delete(self):
        index = self._selected_compare_index()
        if index is not None:
            self.selection.remove(index)

    def on_compare_reset(self):
        self.selection.reset()

    def on_compare_move(self, delta):
        index = self._selected_compare_index()
        if index is not None and self.selection.move(index, delta):
            self._refresh_compare_list()

    def on_open_comparison(self):
        if self.comparison_view is None:
            from comparison_view import ComparisonView
            self.comparison_view = ComparisonView(self.selection)
        self.comparison_view.show()
        self.comparison_view.raise_()
        self.comparison_view.refresh()

    def prepare_calculation(self, settings):
        """Alle GUI-Raster vollständig übernehmen; keine veralteten Basiswerte."""
        return settings["stoff"], deepcopy(settings["raster"]), deepcopy(settings["visu_raster"])

    def on_export_homogeneous(self):
        viewport = self.workspace.active
        if viewport is None or viewport.diagram_id != "pvt" or not viewport.daten:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Homogene Flächen exportieren",
                                              "homogene_flaechen.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            stats = export_homogeneous_csv(viewport.daten, path)
        except Exception as exc:
            QMessageBox.critical(self, "Export fehlgeschlagen", str(exc))
            return
        self.statusBar().showMessage(
            f"Export: {stats['rows']} Knoten ({stats['by_source']}), Spalte 'source' = HEOS | Ersatz")

    def on_surface_grid_toggled(self, checked):
        viewport = self.workspace.active
        if viewport is not None:
            viewport.set_grid(checked)

    def on_diagram_changed(self, _index=None, *, preserve_status=False):
        ids = self.selected_diagram_ids()
        has_pvt = "pvt" in ids

        # p-v-T-Visualisierungsparameter nur zeigen, wenn p-v-T ausgewaehlt ist.
        for group in self.pvt_parameter_groups:
            group.setEnabled(has_pvt)
            group.setVisible(has_pvt)

        # Automatisches p-v-T-Raster nur mit p-v-T
        self.reset_button.setEnabled(has_pvt and not self.manager.is_running)
        if self.workspace.active is None:
            self._refresh_layer_panel()
        if hasattr(self, "isoline_group"):
            self._refresh_isoline_panel()
        if not preserve_status:
            if self.mode == MODE_SINGLE:
                text = self.diagram_combo.currentText()
            else:
                text = ", ".join(DIAGRAMS.get(i).label for i in ids) or "Kein Diagramm"
            self.statusBar().showMessage(f"{text} ausgewählt. ""Berechnung noch nicht gestartet.")
