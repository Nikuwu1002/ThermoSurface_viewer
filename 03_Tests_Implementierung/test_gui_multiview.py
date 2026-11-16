"""TS-A02..A06: Registry, Cache, Berechnungsmanager, Kachelraster und Maximierung.

Die GUI-Tests brauchen ein echtes OpenGL-Fenster (kein QT_QPA_PLATFORM=offscreen) sowie CoolProp
und PyVistaQt; ohne diese werden sie uebersprungen. Reine Logiktests laufen immer.
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

import numpy as np

from diagram_cache import DiagramKey, DiagramResultCache, estimate_nbytes, params_hash
from diagram_registry import DIAGRAMS, LAYER_LABELS, DiagramRegistry, DiagramSpec
from tile_layout import tile_grid_shape

GUI_SKIP = None
MAIN_SKIP = None
try:
    os.environ.setdefault("QT_API", "pyside6")
    import main as app_main
except Exception as exc:  # pragma: no cover
    MAIN_SKIP = f"main.py nicht importierbar: {exc}"
if os.environ.get("QT_QPA_PLATFORM", "").lower() in ("offscreen", "minimal"):
    GUI_SKIP = "GUI-Tests brauchen eine OpenGL-faehige Plattform (nicht offscreen/minimal)"
elif MAIN_SKIP:
    GUI_SKIP = MAIN_SKIP
else:
    try:
        import CoolProp  # noqa: F401
        import pyvistaqt  # noqa: F401
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QMessageBox
        from diagram_viewport import DiagramViewport
    except Exception as exc:  # pragma: no cover
        GUI_SKIP = f"GUI-Abhaengigkeiten fehlen: {exc}"


class RegistryTests(unittest.TestCase):
    def test_seven_types_registered_in_order(self):
        self.assertEqual(DIAGRAMS.ids(), ["pvt", "pht", "pst", "hsp", "uvt", "gpt", "fvt"])
        for spec in DIAGRAMS:
            self.assertTrue(set(spec.layers) <= set(LAYER_LABELS))
            state = spec.initial_layer_state()
            self.assertEqual(set(state), set(LAYER_LABELS))

    def test_unknown_and_duplicate_ids_rejected(self):
        with self.assertRaises(ValueError):
            DIAGRAMS.get("xyz")
        registry = DiagramRegistry()
        spec = DiagramSpec("a", "A", "render_pvt", ("surface",), frozenset())
        registry.register(spec)
        with self.assertRaises(ValueError):
            registry.register(spec)

    def test_new_diagram_needs_only_registration(self):
        registry = DiagramRegistry()
        registry.register(DiagramSpec("neu", "Neu (3D)", "render_pvt", ("surface",), frozenset({"surface"})))
        self.assertEqual(registry.ids(), ["neu"])


class CacheTests(unittest.TestCase):
    def key(self, diagram_id="pht", **params):
        return DiagramKey.build("Water", diagram_id, params)

    def test_key_ignores_view_state_and_dict_order(self):
        self.assertEqual(self.key(a=1, b=2.5), self.key(b=2.5, a=1))
        self.assertNotEqual(self.key(a=1), self.key(a=2))
        self.assertNotEqual(DiagramKey.build("Water", "pht", {}), DiagramKey.build("CO2", "pht", {}))
        self.assertNotEqual(DiagramKey.build("Water", "pht", {}), DiagramKey.build("Water", "pst", {}))

    def test_numpy_scalars_hash_like_python(self):
        self.assertEqual(params_hash({"n": np.int64(5)}), params_hash({"n": 5}))

    def test_hit_miss_counters_and_lru_budget(self):
        arr = np.zeros(1000)  # 8000 Byte
        cache = DiagramResultCache(max_bytes=20000, warn_bytes=12000)
        keys = [self.key(i=i) for i in range(3)]
        self.assertIsNone(cache.get(keys[0]))
        for k in keys[:2]:
            cache.put(k, {"a": arr.copy()})
        self.assertIs(cache.get(keys[0]) is not None, True)  # keys[0] ist nun zuletzt benutzt
        evicted = cache.put(keys[2], {"a": arr.copy()})
        self.assertEqual(evicted, [keys[1]])  # aeltester Eintrag, nicht der gerade genutzte
        self.assertIn(keys[0], cache)
        self.assertIn(keys[2], cache)
        self.assertEqual(cache.hits, 1)
        self.assertEqual(cache.misses, 1)
        self.assertTrue(cache.warnings)  # Warnschwelle gemeldet, nichts stillschweigend

    def test_latest_entry_survives_even_if_larger_than_budget(self):
        cache = DiagramResultCache(max_bytes=100)
        k = self.key()
        cache.put(k, {"a": np.zeros(1000)})
        self.assertIn(k, cache)

    def test_estimate_nbytes_counts_nested_arrays(self):
        self.assertGreaterEqual(estimate_nbytes({"a": [np.zeros(10), {"b": np.zeros(5)}]}), 120)


@unittest.skipIf(MAIN_SKIP, MAIN_SKIP or "")
class CacheKeyRelevanceTests(unittest.TestCase):
    """A-T04: nur tatsaechlich verwendete Parameter beeinflussen den Cache-Schluessel."""

    def task(self, diagram_id, raster=None, visu=None):
        _, base_raster, base_visu = app_main.lade_konfiguration()
        from calculation_manager import DiagramTask, cache_key_for
        task = DiagramTask(diagram_id, "Water", raster or base_raster, visu or base_visu)
        return cache_key_for(task)

    def test_pvt_only_options_do_not_invalidate_other_diagrams(self):
        _, raster, visu = app_main.lade_konfiguration()
        changed = {k: dict(v) for k, v in visu.items()}
        changed["isochore_visu"]["v_points"] = changed["isochore_visu"].get("v_points", 100) + 7
        self.assertNotEqual(self.task("pvt"), self.task("pvt", visu=changed))
        for diagram_id in ("pht", "pst", "hsp", "uvt"):
            self.assertEqual(self.task(diagram_id), self.task(diagram_id, visu=changed))

    def test_q_points_invalidate_all_five(self):
        _, raster, visu = app_main.lade_konfiguration()
        changed = {k: dict(v) for k, v in visu.items()}
        changed["two_phase_visu"]["Q_points"] += 5
        for diagram_id in DIAGRAMS.ids():
            self.assertNotEqual(self.task(diagram_id), self.task(diagram_id, visu=changed))

    def test_diagram_settings_are_part_of_the_key(self):
        with patch.object(app_main, "lade_diagramm_optionen", lambda i: {"n_p": 41}):
            first = self.task("hsp")
        with patch.object(app_main, "lade_diagramm_optionen", lambda i: {"n_p": 80}):
            second = self.task("hsp")
        self.assertNotEqual(first, second)

    def test_fluid_changes_key(self):
        from calculation_manager import DiagramTask, cache_key_for
        _, raster, visu = app_main.lade_konfiguration()
        self.assertNotEqual(cache_key_for(DiagramTask("pht", "Water", raster, visu)),
                            cache_key_for(DiagramTask("pht", "CO2", raster, visu)))


class GridShapeTests(unittest.TestCase):
    def test_fixed_automatic_arrangement(self):
        self.assertEqual(tile_grid_shape(1), (1, 1))
        self.assertEqual(tile_grid_shape(2), (1, 2))
        self.assertEqual(tile_grid_shape(3), (2, 2))
        self.assertEqual(tile_grid_shape(4), (2, 2))
        self.assertEqual(tile_grid_shape(5), (2, 3))
        self.assertEqual(tile_grid_shape(6), (2, 3))
        self.assertEqual(tile_grid_shape(7), (3, 3))


def spin(app, condition, timeout=180.0):
    end = time.time() + timeout
    while not condition():
        app.processEvents()
        time.sleep(0.01)
        if time.time() > end:
            raise TimeoutError("Bedingung nicht rechtzeitig erfuellt")


@unittest.skipIf(GUI_SKIP, GUI_SKIP or "")
class MultiViewGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self, failing=()):
        stoff, raster, visu = app_main.lade_konfiguration()
        self.calls = []

        def berechnen(stoff_, raster_, visu_, diagram_type="pvt", progress_callback=None):
            self.calls.append(diagram_type)
            if diagram_type in failing:
                raise RuntimeError(f"absichtlicher Fehler {diagram_type}")
            return app_main.berechne_auswahl(stoff_, raster_, visu_, diagram_type=diagram_type,
                                             progress_callback=progress_callback)

        window = app_main.ThermoSurfaceWindow(
            start_stoff=stoff, surface_cfg=raster["isotherm_grid"], raster=raster, visu_raster=visu,
            berechnen=berechnen, grid_generator=app_main.generate_stoff_grid)
        window.show()
        self.addCleanup(self.close_window, window)
        return window

    def close_window(self, window):
        window.manager.shutdown()
        window.close()
        self.app.processEvents()

    def calculate(self, window):
        window.on_calculate_clicked()
        spin(self.app, lambda: not window.manager.is_running)
        self.app.processEvents()

    def select(self, window, ids):
        window.mode_combo.setCurrentIndex(window.mode_combo.findData("multi"))
        for diagram_id, box in window.diagram_checks.items():
            box.setChecked(diagram_id in ids)

    def test_single_mode_is_default_and_unchanged(self):
        window = self.make_window()
        self.assertEqual(window.mode_combo.currentData(), "single")
        self.assertEqual([window.diagram_combo.itemData(i) for i in range(window.diagram_combo.count())],
                         DIAGRAMS.ids())
        self.assertFalse(window.diagram_combo.isHidden())
        self.assertTrue(window.diagram_checks_widget.isHidden())
        window.diagram_combo.setCurrentIndex(window.diagram_combo.findData("pht"))
        window.on_confirm_stoff()
        self.calculate(window)
        self.assertEqual(self.calls, ["pht"])
        self.assertEqual(list(window.workspace.viewports), ["pht"])
        viewport = window.workspace.viewports["pht"]
        self.assertTrue(viewport.header.isHidden())
        self.assertTrue(viewport.has_result)
        self.assertTrue(window.layer_checks["surface"].isEnabled())
        self.assertTrue(window.layer_checks["isothermen"].isVisible())  # TS-VP03: p-h-T besitzt jetzt Isolinien-Layer

    def test_single_mode_all_types_and_fluid_change(self):
        # A-T01 / A-T04: jeder Typ einzeln, Cache bei Wiederholung, Neuberechnung bei Stoffwechsel
        window = self.make_window()
        window.on_confirm_stoff()
        for diagram_id in DIAGRAMS.ids():
            window.diagram_combo.setCurrentIndex(window.diagram_combo.findData(diagram_id))
            window.on_confirm_stoff()
            self.calculate(window)
            self.assertEqual(list(window.workspace.viewports), [diagram_id])
            viewport = window.workspace.viewports[diagram_id]
            self.assertTrue(viewport.has_result)
            self.assertTrue(window.layer_checks["surface"].isChecked())
            self.assertEqual(viewport.available_layers()["critical_point"], True)
        self.assertEqual(self.calls, DIAGRAMS.ids())

        window.diagram_combo.setCurrentIndex(window.diagram_combo.findData("pht"))
        self.calculate(window)
        self.assertEqual(len(self.calls), len(DIAGRAMS))  # Cache-Treffer, keine neue Berechnung
        self.assertEqual(window.layer_checks["spinodale"].isVisible(), False)

        window.stoff_combo.setCurrentIndex(window.stoff_combo.findData("CO2"))
        window.on_confirm_stoff()
        self.calculate(window)
        self.assertEqual(self.calls[-1], "pht")
        self.assertEqual(len(self.calls), len(DIAGRAMS) + 1)  # Stoffwechsel -> Neuberechnung

    def test_multi_without_selection_is_rejected(self):
        window = self.make_window()
        self.select(window, [])
        with patch.object(QMessageBox, "warning") as warning:
            window.on_calculate_clicked()
        warning.assert_called_once()
        self.assertFalse(window.manager.is_running)
        self.assertEqual(self.calls, [])

    def test_layouts_independence_maximize_cache_and_errors(self):
        window = self.make_window()
        window.on_confirm_stoff()
        workspace = window.workspace

        # A-T02: 2, 3, 4, 5 Diagramme, Anordnung
        expected = {2: (1, 2), 3: (2, 2), 4: (2, 2), 5: (2, 3)}
        order = ["pht", "pst", "hsp", "uvt", "pvt"]
        for count in (2, 3, 4, 5):
            ids = order[:count]
            self.select(window, ids)
            window.on_confirm_stoff()
            self.calculate(window)
            self.assertEqual(list(workspace.viewports), [i for i in DIAGRAMS.ids() if i in ids])
            positions = {i: workspace._grid.getItemPosition(workspace._grid.indexOf(v))[:2]
                         for i, v in workspace.viewports.items()}
            rows, cols = expected[count]
            self.assertEqual(max(r for r, _ in positions.values()) + 1, rows)
            self.assertLessEqual(max(c for _, c in positions.values()) + 1, cols)
            for viewport in workspace.viewports.values():
                self.assertTrue(viewport.isVisible())
        # jedes Diagramm genau einmal berechnet (Cache-Treffer bei Erweiterung)
        self.assertEqual(sorted(self.calls), sorted(order))
        self.assertEqual(window.manager.compute_calls, 5)
        self.assertIsNone(window.manager._thread)

        viewports = workspace.viewports

        # A-T02/A-T04: Kamera und Layer unabhaengig
        cameras = {i: v.camera_state() for i, v in viewports.items()}
        viewports["pht"].plotter.camera.azimuth = 40.0
        viewports["pht"].plotter.render()
        self.assertNotEqual(viewports["pht"].camera_state(), cameras["pht"])
        for diagram_id in ("pst", "hsp", "uvt", "pvt"):
            self.assertEqual(viewports[diagram_id].camera_state(), cameras[diagram_id])
        workspace.set_active(viewports["pst"])
        window.layer_checks["binodale"].setChecked(False)
        self.assertFalse(viewports["pst"].layer_state["binodale"])
        self.assertTrue(viewports["pht"].layer_state["binodale"])
        self.assertTrue(all(a.visibility is False for a in viewports["pst"].actors["binodale"]))
        self.assertTrue(all(a.visibility for a in viewports["pht"].actors["binodale"]))
        cameras = {i: v.camera_state() for i, v in viewports.items()}
        layer_states = {i: dict(v.layer_state) for i, v in viewports.items()}
        calls_before = len(self.calls)
        render_counts = {i: v.render_count for i, v in viewports.items()}

        # A-T03: Doppelklick (echtes Qt-Ereignis), anderes Diagramm, Return - ohne Berechnung
        QTest.mouseDClick(viewports["hsp"].plotter, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertIs(workspace.maximized, viewports["hsp"])
        self.assertTrue(workspace.return_button.isVisible())
        self.assertTrue(viewports["hsp"].isVisible())
        self.assertTrue(all(not viewports[i].isVisible() for i in viewports if i != "hsp"))
        self.assertIs(workspace.active, viewports["hsp"])
        workspace.restore()
        self.assertTrue(all(v.isVisible() for v in viewports.values()))
        self.assertFalse(workspace.return_button.isVisible())
        workspace.maximize(viewports["pvt"])
        workspace.return_button.click()
        self.app.processEvents()
        self.assertIsNone(workspace.maximized)
        self.assertEqual(len(self.calls), calls_before)
        self.assertEqual({i: v.camera_state() for i, v in viewports.items()}, cameras)
        self.assertEqual({i: dict(v.layer_state) for i, v in viewports.items()}, layer_states)
        self.assertEqual({i: v.render_count for i, v in viewports.items()}, render_counts)

        # A-T04: erneut berechnen -> 0 neue Aufrufe, Viewports unveraendert
        self.calculate(window)
        self.assertEqual(len(self.calls), calls_before)
        self.assertEqual({i: v.render_count for i, v in workspace.viewports.items()}, render_counts)
        self.assertEqual({i: v.camera_state() for i, v in workspace.viewports.items()}, cameras)

        # A-T04: nur relevanter p-v-T-Parameter -> genau ein Cache-Miss
        field = window.grid_fields["visu_points_creation"]["isochore_visu"]
        key = next(k for k in field if k != "v_spacing")
        control = field[key]
        control.setValue(control.value() * 1.1 if control.value() else 1.0)
        self.calculate(window)
        self.assertEqual(self.calls[calls_before:], ["pvt"])

        # Moduswechsel: Ansicht zurueck, Cache bleibt, Rueckkehr ohne HEOS
        calls_before = len(self.calls)
        window.mode_combo.setCurrentIndex(window.mode_combo.findData("single"))
        self.app.processEvents()
        self.assertEqual(len(workspace.viewports), 0)
        self.assertGreaterEqual(len(window.cache), 5)
        window.diagram_combo.setCurrentIndex(window.diagram_combo.findData("uvt"))
        self.calculate(window)
        self.assertEqual(len(self.calls), calls_before)
        self.assertEqual(list(workspace.viewports), ["uvt"])

        # Wiederholtes An-/Abwaehlen: keine Viewport-Reste, keine Fehler
        for i in range(6):
            self.select(window, ["pht", "pst"] if i % 2 else ["pst", "uvt", "hsp"])
            self.calculate(window)
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertEqual(len(window.findChildren(DiagramViewport)), len(workspace.viewports))
        self.assertEqual(len(self.calls), calls_before)

    def test_failed_diagram_does_not_block_others(self):
        window = self.make_window(failing=("hsp",))
        window.on_confirm_stoff()
        self.select(window, ["pht", "hsp", "uvt"])
        with patch.object(QMessageBox, "warning") as warning, patch.object(QMessageBox, "critical") as critical:
            self.calculate(window)
        self.assertEqual(sorted(window.workspace.viewports), ["pht", "uvt"])
        self.assertEqual(warning.call_count + critical.call_count, 1)
        message = (warning.call_args or critical.call_args)[0][2]
        self.assertIn("absichtlicher Fehler hsp", message)
        # Fehler wird nicht gecacht; erfolgreiche Diagramme sind es
        self.assertEqual(len(window.cache), 2)

    def test_start_blocked_while_running_and_thread_released(self):
        window = self.make_window()
        window.on_confirm_stoff()
        self.select(window, ["pst", "uvt"])
        window.on_calculate_clicked()
        self.assertTrue(window.manager.is_running)
        self.assertFalse(window.calculate_button.isEnabled())
        window.on_calculate_clicked()  # zweite Startaktion wird ignoriert
        spin(self.app, lambda: not window.manager.is_running)
        self.assertEqual(sorted(self.calls), ["pst", "uvt"])
        self.assertIsNone(window.manager._thread)
        self.assertTrue(window.calculate_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
