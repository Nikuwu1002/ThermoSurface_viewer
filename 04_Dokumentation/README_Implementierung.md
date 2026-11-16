# ThermoSurface – Schritt-für-Schritt-Implementierung der Korrekturen

**Stand:** 21.10.2026  
**Basis:** bereitgestellter Projektstand (CoolProp HEOS, PyVista, PySide6)  
**Wichtig:** Diese Arbeitskopie wurde syntaktisch und mit synthetischen Modellen getestet. **Echte HEOS-Läufe und die grafische Darstellung sind in der verfügbaren Umgebung mangels CoolProp/PyVista/PySide6 nicht überprüfbar.** Die Änderungen sind daher ein prüfbarer Implementierungsvorschlag, keine vollständige Abnahme für reale Fluide.

Die aktuelle Ordnerstruktur und die dazu passenden Start- und Testbefehle stehen in [README_Projektstruktur.md](README_Projektstruktur.md). Die unten genannten Dateipfade und Befehle dokumentieren den damaligen Stand.

## 0. Vorbereitung und Sicherung

1. Gesamten bisherigen Projektordner sichern, z. B. `ThermoSurface_backup_2026-10-21/`.
2. Neue Arbeitskopie `ThermoSurface_korrigiert/` erstellen.
3. Alle ursprünglichen `.py`-Dateien und `config.yaml` in dieser Arbeitskopie belassen.
4. Aus diesem Korrekturpaket **nur die unten genannten geänderten Dateien** in die Arbeitskopie kopieren, nicht in das ungeprüfte Original.
5. Tests zuerst ohne und danach mit installiertem CoolProp/PyVista/PySide6 ausführen.

Die geänderten Dateien: `homogeneous_pvt.py`, `visualization.py`, `main.py`, `gui.py`, `Debug.py`, `config.yaml`, `snapshot_pvt.py`, `compare_pvt.py`, `validate_ap17_ap18.py`. Neu: `test_corrections.py`, `diagnose_resolution.py`.

> Die bereits vorhandenen anderen Python-Module wie `coolprop_heos_pvt.py`, `spinodal_vapor_solver.py`, `coolprop_heos_common.py` bleiben unverändert.

## 1. Punktgenaue Diagnose in `homogeneous_pvt.py` und `Debug.py`

**Ausgangsproblem:** Bislang konnte ein einzelner Fehler während der Berechnung des inneren Volumenrasters eine gesamte Temperaturzeile entfernen. Die Ursache war dann nur als allgemeiner Zeilenfehler dokumentiert.

**Umsetzung:**

1. Funktion `build_homogeneous_surfaces()` in `homogeneous_pvt.py` durch die gleichnamige korrigierte Funktion ersetzen.
2. Die Auswertung der beiden Randpunkte wird **vor** der inneren Punktberechnung durchgeführt. Ungültige/inkonsistente Randpunkte sperren nach wie vor die gesamte Temperaturzeile.
3. Innere Volumenpunkte werden jeweils einzeln in einem eigenen `try`/`except` berechnet. Fehlgeschlagene innere Punkte erhalten einen `NaN`-Druck, gültige Nachbarn bleiben erhalten.
4. `reports[name]` enthält zusätzlich `boundary_rows`, `partial_rows`, `valid_nodes`, `invalid_nodes`, `nonpositive_pressures` und `point_reasons`.
5. `Debug.diagnose_homogeneous()` protokolliert vollständige und partielle Zeilen, gültige Punkte, nichtpositive Drücke und potenzielle Dreiecke. Einzelne `point_reasons` werden im Debug-Modus protokolliert.

**Abnahme:** Ein gezielt ausgelöster innerer Berechnungsfehler erzeugt eine partielle Zeile; die Randpunkte und die übrigen inneren Punkte bleiben bestehen. Test: `python -m unittest test_corrections.Corrections.test_point_error_does_not_remove_entire_row -v`.

## 2. Randvalidierung korrigieren

**Ausgangsproblem:** `validate_homogeneous_boundaries()` meldete eine exakte Randübereinstimmung auch dann, wenn gar kein Rand auswertbar war.

**Umsetzung:**

1. Die gleichnamige Funktion in `homogeneous_pvt.py` ersetzen.
2. Auswertbar ist eine Zeile für die Randprüfung, wenn beide Randpunkte endlich sind (`boundary_rows`).
3. `exact_boundary` ist nur dann `True`, wenn mindestens eine Randzeile existiert und die exakte Übereinstimmung nachgewiesen wurde; bei 0 Randzeilen ist es `False`.
4. Die Funktion überprüft bei partiellen Zeilen die Volumenmonotonie und die identischen Binodal-/Spinodalknoten ebenfalls.

**Abnahme:** `python -m unittest test_corrections.Corrections.test_no_false_success_for_empty_surface -v`.

## 3. Einheitliche und optional vorzeichenfähige Druckachse

**Problem:** `log10(p/pc)` kann `p <= 0` nicht darstellen; negative Zustandsdrücke werden nicht zu null oder `p_sat` gemacht, sondern waren im Renderer bisher ausgeblendet.

**Umsetzung:**

1. In `config.yaml` unter `diagram_settings: pvt:` ist das neue Feld `pressure_axis: "log"` enthalten.
2. Als Alternative `pressure_axis: "asinh"` setzen.
3. `visualization.to_xyz(points, critical, pressure_axis)` nutzt entsprechend
   - `log`: `z = log10(p/pc)`; Darstellbarkeit nur bei `p > 0`.
   - `asinh`: `z = arcsinh(p/pc)`; stetig definiert auch für `p <= 0`.
4. `main.berechne_daten()` liest das Feld ein und speichert es in `daten["pressure_axis"]`.
5. **Zwingend:** Die neue Koordinatentransformation wird in **allen** p-v-T-Renderpfaden verwendet, nicht nur bei den instabilen Flächen: Einphasenfläche, Zweiphasenfläche samt kritischem Abschluss, Binodalen, Spinodalen, Isolinien und kritischer Punkt.
6. Die Achsenbeschriftung wird abhängig vom Modus korrekt angepasst.

**Wichtig:** Die asinh-Variante ist eine **Darstellungstransformation**, keine thermodynamische Korrektur. Die ursprünglichen SI-Druckwerte bleiben unverändert. Andere Diagrammtypen verwenden unverändert ihre bisherigen Achsen.

**Abnahme:** `python -m unittest test_corrections.Corrections.test_3_corner_mesh_and_signed_pressure_axis -v`.

## 4. PyVista-Vernetzung und Flächenverfügbarkeit

**Problem:** Alte `homogeneous_surface_mesh()`-Logik verlangte vier gültige Ecken für beide Dreiecke einer Rasterzelle. Zwei nicht zusammenhängende gültige Temperaturzeilen reichten in `main.py` trotzdem für ein positives Verfügbarkeitssignal.

**Umsetzung:**

1. Funktion `visualization.homogeneous_surface_mesh()` durch die korrigierte Implementierung ersetzen.
2. Nur Rasterzellen mit 3 oder 4 gültigen Ecken dürfen Dreiecke liefern: 3 Ecken → 1 Dreieck, 4 Ecken → 2 Dreiecke. Wenn weniger als 3 Ecken vorliegen, keine Verbindung erzeugen.
3. Geometrisch degenerierte Dreiecke (Flächeninhalt 0) aussortieren.
4. In `homogeneous_pvt.py` ist `count_renderable_triangles()` hinzugekommen. Die Funktion ermittelt ohne VTK aus dem gleichen Gültigkeitskriterium die potenzielle Dreiecksanzahl.
5. In `main.berechne_daten()` `hom_reports["available"]` nicht mehr aus `valid_rows >= 2` bestimmen. Stattdessen für jedes Netz `candidate_triangles` berechnen und auf `> 0` prüfen.
6. Der GUI-Tooltip zeigt jetzt vollständige/partielle/fehlende Zeilen und die Dreiecksanzahl.

**Abnahme:** Die Tests `test_no_triangle_between_isolated_rows` und `test_3_corner_mesh_and_signed_pressure_axis` bestehen. In PyVista müssen `mesh.n_cells` und die tatsächlichen Dreiecksflächen zusätzlich überprüft werden.

## 5. Spinodalen und kritische Randzone

1. `visualization.add_spinodals()` verwendet zwingend die neuen homogenen Spinodalen, **wenn** eine zugehörige metastabile Fläche erzeugbare Zellen besitzt; damit gibt es keine stille Vermischung mit Legacy-Spinodalen.
2. Legacy-Kurven bleiben Fallback, wenn für den zugehörigen Ast keine neue homogene Fläche zur Verfügung steht.
3. Die vorhandene Funktion `solve_spinodal_branch()` und die gemeinsame Temperaturbasis wurden **nicht physikalisch neu modelliert**. Ihre Resultate müssen für reale Stoffe separat validiert werden.
4. Das Diagnoseskript `diagnose_resolution.py` gibt `last_subcritical_gap_K` an. Ein Abstand zum kritischen Punkt ist kein Beweis eines Rechenfehlers.
5. **Keine künstliche Verlängerung von Spinodalen zum kritischen Punkt**, bevor ein belastbarer Grenzwert- und Phasenkonsistenztest vorliegt. Genau hier ist die reale HEOS-Abnahme ausständig.

**Abnahme:** Für jede Temperatur mit renderbarem metastabilem Netz prüfen: Spinodal-Randpunkte des Netzes sind im gemeinsamen `(T,v,p)`-Array identisch, Ableitungsresiduum unter der begründeten Toleranz und richtige Reihenfolge `v_sat,l < v_spin,l < v_spin,g < v_sat,g`. Im kritischen Grenzbereich zusätzliche Temperaturstützstellen nur nach nachgewiesener Solverrobustheit verwenden.

## 6. Regression und Auflösungskonvergenz

1. `snapshot_pvt.py` enthält jetzt neue homogene Arrays und die beiden neuen Spinodalen; die neuen Python-Quelldateien werden mitgehasht.
2. `compare_pvt.py` und `snapshot_pvt.py` verwenden Snapshot-Schema **Version 2**. Alte Baselines nicht stillschweigend als kompatibel behandeln. Frühere Baselines archivieren.
3. Nur nach bestandener realer HEOS-Prüfung eine neue Baseline bewusst freigeben:

   ```powershell
   python snapshot_pvt.py baseline --approve-baseline
   python snapshot_pvt.py nach_korrektur
   python compare_pvt.py nach_korrektur
   ```

   Achtung: Wenn `regression/baseline` bereits existiert, wird absichtlich **nicht überschrieben**. Sie muss zuerst unverändert archiviert und im neuen Schema bewusst neu angelegt werden.

4. Mit `diagnose_resolution.py` die homogenen Flächen bei identischen Temperaturen und Spinodalen, aber verschiedenen Volumenauflösungen vergleichen:

   ```powershell
   python diagnose_resolution.py --fluid Water --nT 24 --nscan 160 --output results/water_resolution.json
   python diagnose_resolution.py --fluid CO2 --nT 24 --nscan 160 --output results/co2_resolution.json
   python diagnose_resolution.py --fluid R134a --nT 24 --nscan 160 --output results/r134a_resolution.json
   python diagnose_resolution.py --fluid Argon --nT 24 --nscan 160 --output results/argon_resolution.json
   ```

   Für die zweite Dimension der Konvergenz `--nT` von 24 auf 48 erhöhen und die Unterschiede separat dokumentieren. Voreinstellung der Volumenpunkte: 5, 20, 40. Bei problematischen Temperaturen mit `--tmin` und `--tmax` den Bereich gezielt eingrenzen.

5. Gesamt-Testsuite:

   ```powershell
   python -m compileall -q .
   python -m unittest discover -v
   python validate_ap17_ap18.py --output results/ap17_ap18.json
   ```

**Abnahme:** Für Wasser, CO₂, R134a und Argon nachvollziehbare Berichte mit gültigen Spinodalen, randkonformen Flächen, Zahlen der gültigen/partiellen Temperaturzeilen und tatsächlichen PyVista-Dreiecken. Zusätzliche GUI-Klicktests im tatsächlichen Zielsystem erforderlich.

## 7. Erwartete Ergebnisse und verbleibende Risiken

- Einzelne innere HEOS-Fehler löschen nicht mehr alle Druckwerte derselben Temperaturzeile.
- Aus partiellen Rasterzellen können kleinere gültige Dreiecke entstehen.
- Negative Drücke können im globalen `asinh`-Darstellungsmodus sichtbar werden.
- Randkonsistenz wird nicht mehr bei leeren Netzen als bestanden ausgegeben.
- Reale HEOS-Stabilitätsprobleme werden **nicht** durch Interpolation, erzwungene Positivdrücke oder erfundene kritische Endpunkte verborgen.
- Die physikalische Verfügbarkeit der homogenen Fortsetzung unterhalb der Spinodalen, mögliche zusätzliche HEOS-Extrema, Gültigkeitsgrenzen und optische Glättung bleiben an realen Fluiden zu prüfen.

**Lokales Testergebnis der beigefügten Arbeitskopie:** 22 Tests entdeckt, 19 erfolgreich, 3 übersprungen (2 reale HEOS-Tests mangels CoolProp, 1 Testklasse mangels nicht mitgelieferter `Test.py`). Python-Syntaxprüfung bestanden. **Kein realer HEOS-/PyVista-/GUI-End-to-End-Test durchgeführt.**
