"""TS-B07 (GUI): Punktauswahl per echtem Mausklick, Marker, Vergleichsmodus, Loeschen/Reihenfolge, Stoffwechsel.

Braucht ein echtes OpenGL-Fenster (nicht QT_QPA_PLATFORM=offscreen) und wird sonst uebersprungen.
"""
import time
import unittest

from test_gui_multiview import GUI_SKIP, app_main, spin

if not GUI_SKIP:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from state_picker import PickError, pick_from_viewport


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


@unittest.skipIf(GUI_SKIP, GUI_SKIP or "")
class B07GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self):
        stoff, raster, visu = app_main.lade_konfiguration()
        window = app_main.ThermoSurfaceWindow(
            start_stoff=stoff, surface_cfg=raster["isotherm_grid"], raster=raster, visu_raster=visu,
            berechnen=app_main.berechne_auswahl, grid_generator=app_main.generate_stoff_grid)
        window.show()
        self.addCleanup(self.close_window, window)
        return window

    def close_window(self, window):
        window.manager.shutdown()
        window.close()
        self.app.processEvents()

    def calculate(self, window, ids):
        window.mode_combo.setCurrentIndex(window.mode_combo.findData("multi"))
        for diagram_id, box in window.diagram_checks.items():
            box.setChecked(diagram_id in ids)
        window.on_calculate_clicked()
        spin(self.app, lambda: not window.manager.is_running)
        self.app.processEvents()

    def find_hit(self, window, viewport, preferred=None):
        """Qt-Pixel, an dem der VTK-Picker eine echte Zustandsflaeche trifft (gleiche Umrechnung wie der Viewport)."""
        key = viewport.result_key
        index = window.state_store.ensure(key.fluid, viewport.diagram_id, viewport.daten, key)
        width, height = viewport.plotter.width(), viewport.plotter.height()
        ratio = viewport.plotter.devicePixelRatioF()
        for fy in (0.5, 0.4, 0.6, 0.3, 0.7):
            for fx in (0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8):
                qx, qy = int(width * fx), int(height * fy)
                try:
                    pick = pick_from_viewport(viewport, index, int(round(qx * ratio)),
                                              int(round((height - 1 - qy) * ratio)))
                except PickError:
                    continue
                if preferred is None or pick.actor_key in preferred:
                    return qx, qy
        self.fail("Kein pickbarer Pixel gefunden")

    def click(self, viewport, qx, qy, wait=0.8):
        QTest.mouseClick(viewport.plotter, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(qx, qy))
        pump(self.app, wait)   # Einfachklick wird erst nach dem Doppelklick-Intervall gemeldet

    def test_picking_inactive_until_mode_enabled(self):
        # Vergleichsmodus aus: keine Auswertung
        window = self.make_window()
        window.on_confirm_stoff()
        self.calculate(window, ["pht"])
        viewport = window.workspace.viewports["pht"]
        qx, qy = self.find_hit(window, viewport)
        before = window.state_store.evaluations
        self.click(viewport, qx, qy)
        self.assertEqual(len(window.selection), 0)
        self.assertEqual(window.state_store.evaluations, before)

    def test_click_selects_marks_and_double_click_only_maximizes(self):
        window = self.make_window()
        window.on_confirm_stoff()
        self.calculate(window, ["pvt", "hsp"])
        window.compare_check.setChecked(True)
        pvt, hsp = window.workspace.viewports["pvt"], window.workspace.viewports["hsp"]

        qx, qy = self.find_hit(window, hsp)
        self.click(hsp, qx, qy)
        self.assertEqual(len(window.selection), 1)
        self.assertEqual(window.selection.points[0].source_diagram, "hsp")
        self.assertEqual(hsp.marker_count, 1)
        self.assertEqual(pvt.marker_count, 0)
        self.assertEqual(window.compare_list.count(), 1)

        qx, qy = self.find_hit(window, pvt)
        self.click(pvt, qx, qy)
        self.assertEqual(len(window.selection), 2)
        self.assertEqual({p.source_diagram for p in window.selection.points}, {"hsp", "pvt"})
        self.assertEqual((pvt.marker_count, hsp.marker_count), (1, 1))
        self.assertEqual({p.state.fluid for p in window.selection.points}, {window.confirmed_stoff})

        # Doppelklick maximiert und waehlt keinen Punkt
        QTest.mouseDClick(hsp.plotter, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(qx, qy))
        pump(self.app, 0.8)
        self.assertEqual(len(window.selection), 2)
        self.assertIs(window.workspace.maximized, hsp)
        window.workspace.restore()
        pump(self.app, 0.8)
        # Vergleichsdiagramm: Achsentausch ohne neue Thermodynamikauswertung
        window.on_open_comparison()
        view = window.comparison_view
        self.assertEqual(len([r for r in view.geometry.rows if r.xyz is not None]), 2)
        evaluations = window.state_store.evaluations
        view.set_axes("T", "p", "v")
        view.set_axes("v", "T", "p")
        self.assertEqual(window.state_store.evaluations, evaluations)
        self.assertEqual(len(set(view.axes)), 3)

        # Reihenfolge, Loeschen (Nummern bleiben eindeutig), Reset
        first, second = [p.selection_index for p in window.selection.points]
        window.compare_list.setCurrentRow(0)
        window.on_compare_move(+1)
        self.assertEqual([p.selection_index for p in window.selection.points], [second, first])
        window.compare_list.setCurrentRow(0)
        window.on_compare_delete()
        self.assertEqual([p.selection_index for p in window.selection.points], [first])
        self.assertEqual(sum(v.marker_count for v in window.workspace.ordered()), 1)
        qx, qy = self.find_hit(window, hsp)
        self.click(hsp, qx, qy)
        self.assertNotIn(window.selection.points[-1].selection_index, (first, second))
        window.on_compare_reset()
        self.assertEqual(len(window.selection), 0)
        self.assertEqual(sum(v.marker_count for v in window.workspace.ordered()), 0)

    def test_exit_releases_threads_views_and_persists_no_selection(self):
        # B-T07: Verlassen der Anwendung
        import glob
        import os
        import threading
        window = self.make_window()
        window.on_confirm_stoff()
        self.calculate(window, ["pht", "pst"])
        window.compare_check.setChecked(True)
        viewport = window.workspace.viewports["pht"]
        qx, qy = self.find_hit(window, viewport)
        self.click(viewport, qx, qy)
        self.assertEqual(len(window.selection), 1)
        window.on_open_comparison()
        view = window.comparison_view
        viewports = list(window.workspace.ordered())
        files_before = set(glob.glob("cache/*")) if os.path.isdir("cache") else set()

        window.close()
        self.app.processEvents()
        self.assertFalse(window.manager.is_running)
        self.assertIsNone(window.comparison_view)
        self.assertEqual(window.workspace.viewports, {})
        self.assertTrue(all(v.plotter._closed for v in viewports + [view]))
        self.assertFalse(window.state_store.config.disk_enabled)
        files_after = set(glob.glob("cache/*")) if os.path.isdir("cache") else set()
        self.assertEqual(files_before, files_after)   # keine Auswahl/kein Zustand auf der Platte
        self.assertFalse([t for t in threading.enumerate() if t.name.startswith("QThread")])

    def test_fluid_change_clears_selection(self):
        window = self.make_window()
        window.on_confirm_stoff()
        self.calculate(window, ["pht"])
        window.compare_check.setChecked(True)
        viewport = window.workspace.viewports["pht"]
        qx, qy = self.find_hit(window, viewport)
        self.click(viewport, qx, qy)
        self.assertEqual(len(window.selection), 1)
        other = "CO2" if window.confirmed_stoff != "CO2" else "Water"
        window.stoff_combo.setCurrentIndex(window.stoff_combo.findData(other))
        window.on_confirm_stoff()
        self.assertEqual(len(window.selection), 0)
        self.assertEqual(window.compare_list.count(), 0)


if __name__ == "__main__":
    unittest.main()
