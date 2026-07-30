# Selection Intersector -- QGIS 3.32 plugin skeleton

Draw a polygon, pick layers or layer groups, get output layer(s) of
everything that intersects. The output stays in sync as source layers
change or the polygon is redrawn.

This is a working skeleton, not a finished product -- see "Known
simplifications" at the bottom for what's deliberately left simple
and how to extend it.

## Files

| File | Purpose |
|---|---|
| `metadata.txt` | Plugin metadata QGIS reads to list it in the plugin manager |
| `__init__.py` | `classFactory()` entry point QGIS calls on load |
| `intersection_plugin.py` | Wires everything together: toolbar action, dock, map tool, engine, watcher |
| `polygon_capture_tool.py` | Click-to-draw-a-polygon map tool |
| `layer_selector_panel.py` | Dock widget: layer/group tree, buttons, status |
| `intersection_engine.py` | The actual geometry work + output-layer management |
| `reactive_watcher.py` | Connects to the right signals to detect "something changed" |

## Architecture, in one pass

- **Drawing**: a custom `QgsMapTool` (not `QgsMapToolEmitPoint`, which
  is for single press/release captures) accumulates clicked vertices
  into a `QgsRubberBand` and emits a `QgsGeometry` polygon when you
  right-click/Enter to finish.
- **Layer/group selection**: a `QTreeWidget` mirrors the project's
  real layer tree (`QgsProject.instance().layerTreeRoot()`), filtered
  to vector layers. Group checkboxes use Qt's built-in "auto
  tristate" flag, so checking a group checks all its layers and the
  group box shows partial/full state automatically -- no extra code
  needed to support "select a whole group" vs "select individual
  layers".
- **Computation**: for each selected layer, the polygon is reprojected
  into that layer's CRS and used as a `QgsFeatureRequest.setFilterRect()`
  bounding-box prefilter -- for a PostGIS layer this gets pushed down
  to the database and uses the table's GiST spatial index, so you're
  not pulling the whole table into QGIS just to test a handful of
  candidates. Each candidate is then precisely tested with
  `QgsGeometry.intersects()` / `.intersection()`.
- **Output**: results are bucketed by geometry type (a layer can only
  hold one) into up to three memory layers -- `<name>_points`,
  `<name>_lines`, `<name>_polygons` -- grouped under an "Intersection
  results" layer group. Each feature carries `source_layer` and
  `source_fid` so you can trace it back.
- **Reactivity**: see `reactive_watcher.py`'s docstring and the
  "How live-updating works" section below.

## Setting up PyCharm against QGIS's Python 3.9 (Windows)

QGIS on Windows bundles its own Python -- the compiled `qgis`, `PyQt5`,
`sip` and `osgeo` modules are built against that exact interpreter, so
PyCharm has to point at *that* Python, not a separately-installed one,
or imports will fail even though the version number matches.

1. Find your QGIS install folder (e.g. `C:\Program Files\QGIS 3.32.2`
   or `C:\OSGeo4W`) and look inside its `bin` folder for a file named
   `python-qgis.bat` or `python-qgis-ltr.bat`. Open it in a text
   editor -- it's short, and it shows you exactly which `PATH`,
   `PYTHONHOME` and `PYTHONPATH` values QGIS itself uses to make its
   Python interpreter work. Folder names shift slightly between
   installer types and versions, so this is more reliable than a
   hardcoded path.
2. Write a small launcher batch file that reuses those same lines and
   then starts PyCharm, e.g.:

   ```bat
   @echo off
   SET QGIS_ROOT=C:\Program Files\QGIS 3.32.2
   call "%QGIS_ROOT%\bin\o4w_env.bat"
   call "%QGIS_ROOT%\bin\qt5_env.bat"
   call "%QGIS_ROOT%\bin\py3_env.bat"
   set PATH=%QGIS_ROOT%\apps\qgis-ltr\bin;%PATH%
   set PYTHONPATH=%QGIS_ROOT%\apps\qgis-ltr\python;%PYTHONPATH%
   start "" "C:\Program Files\JetBrains\PyCharm Community Edition\bin\pycharm64.exe"
   ```

   Adjust `apps\qgis-ltr` to whatever you actually found in step 1
   (could be `apps\qgis`), and the PyCharm path to your install.
3. In PyCharm: File > Settings > Project > Python Interpreter > Add >
   System Interpreter, and point it at `python.exe` (or `python3.exe`)
   inside your QGIS install's Python folder (next to where
   `python-qgis*.bat` lives, often `apps\Python39`).
4. Still in the interpreter settings, open "Show paths for the
   selected interpreter" and add the QGIS `python` folder (the one
   containing the `qgis` package, typically
   `apps\qgis-ltr\python`) so PyCharm's indexer/autocomplete can see
   `import qgis`.
5. Always launch PyCharm via your batch file when working on this
   plugin (not from the Start menu), so the environment variables are
   set before PyCharm's own Python process starts.

Optional but genuinely useful: install the `qgis-stubs` package
(`pip install qgis-stubs`) in that interpreter for much better
autocomplete/type-checking on the PyQGIS API in PyCharm.

## Installing the plugin for development

QGIS loads plugins from your active profile's `python/plugins` folder.
Find it via QGIS: Settings > User Profiles > Open Active Profile
Folder, then go into `python/plugins`. Either copy this
`selection_intersector` folder there, or (better, so edits in PyCharm
show up immediately) create a symlink to it:

```bat
mklink /D "C:\Users\<you>\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\selection_intersector" "C:\path\to\your\pycharm\project\selection_intersector"
```

(Run that from an elevated/Administrator command prompt -- `mklink`
needs it.) Then in QGIS: Plugins > Manage and Install Plugins >
Installed, enable "Selection Intersector".

Install the **Plugin Reloader** plugin (from the QGIS plugin
repository) so you can reload after each code change with one click
instead of restarting QGIS every time.

## How live-updating works

There are three independent triggers, all wired up in
`reactive_watcher.py`:

1. **Edits made in this QGIS session**: the watcher listens to each
   selected layer's *committed* signals (fired when an edit session is
   saved, not on every keystroke of an in-progress edit). This means
   edits are picked up right after you click "Save Edits" -- not
   mid-edit. If you want it to react to uncommitted edits too, swap
   in `featureAdded` / `featureDeleted` / `geometryChanged` (the edit
   buffer's live signals) instead -- one-line change, noted in the
   watcher's docstring.
2. **Layers added/removed from a watched group**: the project's
   layer-tree signals, so adding a new layer into a group you've
   selected picks it up automatically.
3. **Edits from outside this QGIS session** (another user, another
   QGIS instance, a script writing straight to PostgreSQL): this is
   the one case Qt signals on your local layer object can't see by
   themselves, because nothing happened to *your* copy of the layer.
   QGIS actually has a built-in mechanism for exactly this, normally
   exposed as the "Refresh layer on notification" checkbox in a
   PostGIS layer's Properties > Rendering tab: it opens a PostgreSQL
   `LISTEN` connection on a channel called `qgis`, and turns any
   `NOTIFY qgis` into a Qt signal (`QgsVectorDataProvider.notify`).
   The watcher calls `provider.setListening(True)` and connects to
   that signal directly, so you don't need to tick the checkbox by
   hand on every source layer.

   For this to fire, your PostgreSQL tables need a trigger that sends
   the notification. One-time setup per source table:

   ```sql
   CREATE OR REPLACE FUNCTION public.notify_qgis() RETURNS trigger AS $$
   BEGIN
     NOTIFY qgis;
     RETURN NULL;
   END;
   $$ LANGUAGE plpgsql;

   CREATE TRIGGER my_table_notify_qgis
   AFTER INSERT OR UPDATE OR DELETE ON my_schema.my_table
   FOR EACH STATEMENT EXECUTE FUNCTION public.notify_qgis();
   ```

   Note PostgreSQL only sends `NOTIFY` on commit, so this reflects
   committed transactions, same as point 1. Without this trigger,
   external edits are only picked up when someone hits "Recalculate
   now" (always available regardless of auto-update) or another
   watched event happens to fire.

All three funnel into one debounced `sourceDataChanged` signal (400ms,
see `ReactiveWatcher.DEBOUNCE_MS`) so a burst of edits triggers one
recalculation instead of one per feature.

## Known simplifications / where to extend

- **Attributes**: output features only carry `source_layer` and
  `source_fid`, not the original layer's other attributes -- merging
  heterogeneous schemas from different source layers into one output
  table gets messy fast (name collisions, type mismatches). If you
  need full attributes, the cleanest fix is usually one output layer
  *per source layer* (named `<source_layer_name>_intersection`)
  instead of one merged layer per geometry type -- `_write_layer()` is
  the place to change.
- **Large layers / background execution**: `recalculate()` runs
  synchronously on the main thread. For the bounding-box-prefiltered
  approach here this is fast even for large PostGIS tables (the
  database does the filtering), but if you still see it block the UI,
  wrap `_do_recalculate()` in a `QgsTask` (`QgsTask.fromFunction(...)`
  + `QgsApplication.taskManager().addTask(...)`, remembering to keep a
  reference to the task object -- it can get garbage-collected mid-run
  otherwise). See
  https://docs.qgis.org/3.34/en/docs/pyqgis_developer_cookbook/tasks.html.
  Worth testing carefully either way: parts of the Processing
  framework aren't thread-safe for background execution, and this
  code intentionally avoids it in favour of a direct GEOS call
  (`QgsGeometry.intersects()`/`.intersection()`) for exactly that
  reason.
- **Visual persistence of the drawn polygon**: the rubber band clears
  once you finish drawing. If you want the selection polygon to stay
  visible for reference, keep a second, persistent `QgsRubberBand` (or
  a one-feature memory layer) alive in `IntersectionPlugin` alongside
  the working one in `PolygonCaptureTool`.
- **`native:intersection` as an alternative**: `intersection_engine.py`
  does the geometry work directly with GEOS via `QgsGeometry` rather
  than calling `processing.run("native:intersection", ...)`. Both are
  valid; the direct approach here was chosen so the code can report an
  exact `source_fid` per result without guessing at how the algorithm
  renumbers/retains fields. If you'd rather delegate to the processing
  algorithm (e.g. to inherit its exact edge-case/robustness handling,
  or to keep full per-layer attribute schemas via its `INPUT_FIELDS`
  parameter), it takes `INPUT`, `OVERLAY`, `INPUT_FIELDS`,
  `OVERLAY_FIELDS`, `OVERLAY_FIELDS_PREFIX`, `OUTPUT`, `GRID_SIZE`.
- **Multi-user output layer**: the output is an in-memory layer
  (`memory:` provider) that only exists in your local project -- it
  won't itself show up for other users the way a PostGIS-backed output
  table would. If you want to publish the result too, swap
  `_get_or_create_layer()`'s memory layer construction for a
  PostGIS-backed one and write into it instead (same `addFeatures` /
  `deleteFeatures` calls work, just against a different provider).

## Troubleshooting

- `ImportError: No module named 'qgis'` in PyCharm/when running a
  script standalone: PyCharm's interpreter isn't the one bundled with
  QGIS, or the QGIS `python` folder isn't in its interpreter paths --
  revisit the PyCharm setup section above.
- Plugin doesn't appear in Plugins > Manage and Install Plugins: check
  the QGIS Python console (View > Panels > Python Console) for a
  traceback -- a syntax error or bad import in any of these files will
  stop it from loading, and the console shows exactly where.
- Nothing happens when source data changes: confirm "Auto-update on
  changes" is checked, and for PostGIS layers, confirm the `NOTIFY`
  trigger exists on the table you edited (edits via *this* QGIS
  session's own editing tools don't need it -- only external edits
  do).
