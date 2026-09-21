"""
Copy & Correct: copy every feature of one or more layers into a target
layer -- from the project or a database -- reviewing and correcting
attributes one feature at a time.

Workflow:
  1. Highlight one or more layers in the Layers panel (the normal
     Ctrl/Shift-click multi-select QGIS already supports there). No
     feature-level selection is needed -- every feature of each
     highlighted layer is included.
  2. Pick the layer to copy into: an existing project layer, or any
     table from a configured PostgreSQL connection (loaded on demand,
     not added to the project). Layers whose geometry type doesn't
     match what's being copied are shown but can't be picked.
  3. Each feature is copied into the target layer in turn. Its
     attribute form opens automatically, non-modally, so the operator
     can still pan/zoom the map freely while it's open. The feature is
     highlighted on the map for as long as its form is open; the view
     is only re-framed if the feature doesn't already fit inside it --
     an object bigger than the current view gets zoomed out to, one
     that's already visible is left exactly as the operator had it.
  4. Whichever fields the operator actually changed on one feature
     become the starting values pre-filled for the next feature (and
     stay "sticky" until changed again), so a value that repeats
     across many objects only needs to be typed once. Fields nobody
     touched keep each feature's own original value.

The target layer's primary key field(s), if any, are never copied or
carried over -- the data provider assigns those.

Each feature is committed to the target layer right after its form
closes (not batched at the end), so cancelling partway through a run
does not lose the features already confirmed.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor, QStandardItem, QStandardItemModel
from qgis.PyQt.QtWidgets import (
    QAction,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QVBoxLayout,
)

from qgis.core import (
    QgsApplication,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsAttributeDialog, QgsRubberBand

from .db_layers import (
    LongTaskLoader,
    get_connection_credentials,
    get_db_layer_infos,
    get_project_layer_infos,
    resolve_layer,
)


class TargetLayerDialog(QDialog):
    """Confirmation dialog: shows what's selected, picks the target
    layer from either the current project or any configured PostgreSQL
    connection. Entries whose geometry type doesn't match what's being
    copied are shown greyed out and can't be picked."""

    def __init__(self, parent, feature_count, layer_count, layers_info, source_geom_types):
        super().__init__(parent)
        self.setWindowTitle('Копирование объектов')
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            'Выбрано объектов: {0} (слоёв: {1})'.format(feature_count, layer_count)
        ))
        layout.addWidget(QLabel('Слой назначения:'))

        self.layer_combo = QComboBox(self)
        self.model = QStandardItemModel(self.layer_combo)
        self.layer_combo.setModel(self.model)

        # Only grey out mismatches when the copied objects are all one
        # geometry type -- with a mixed batch, any layer might take at
        # least some of them, so nothing is disqualified up front (the
        # per-feature check during the actual copy still applies).
        only_type = next(iter(source_geom_types)) if len(source_geom_types) == 1 else None

        prev_group = None
        for info in layers_info:
            if info.connection_name != prev_group:
                header = QStandardItem('──── {0} ────'.format(info.connection_name))
                header.setFlags(Qt.NoItemFlags)
                self.model.appendRow(header)
                prev_group = info.connection_name

            mismatched = only_type is not None and info.geometry_type != only_type
            text = info.display_name + (' (другой тип геометрии)' if mismatched else '')
            self.layer_combo.addItem(text, userData=info)
            if mismatched:
                item = self.model.item(self.model.rowCount() - 1)
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                item.setToolTip('Тип геометрии слоя не совпадает с копируемыми объектами.')

        layout.addWidget(self.layer_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Ok).setText('Начать')
        buttons.button(QDialogButtonBox.Cancel).setText('Отмена')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_info(self):
        return self.layer_combo.currentData()


class CopyCorrectPlugin:
    """Plugin entry point: registers the toolbar/menu action."""

    MENU_NAME = '&Копирование с проверкой'
    LARGE_BATCH_WARNING = 200  # confirm before opening this many forms one at a time

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self._current_dialog = None
        self._rubber_band = None
        self._review = None  # holds state for the in-progress copy run, if any
        self._connections = []  # cached PostgreSQL connections (with resolved credentials)
        self._layers_info = []  # cached Project + Database target-layer options
        self._task = None
        self._progress_dialog = None
        self._pending_copy_list = None  # copy_list waiting on the async layer load

    def initGui(self):
        icon = QgsApplication.getThemeIcon('mActionEditCopy.svg')
        self.action = QAction(icon, 'Копировать с проверкой атрибутов', self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu(self.MENU_NAME, self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        self.iface.removePluginMenu(self.MENU_NAME, self.action)
        self.iface.removeToolBarIcon(self.action)
        self._abort_review()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._progress_dialog is not None:
            self._progress_dialog.close()
            self._progress_dialog = None

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self):
        if self._review is not None:
            QMessageBox.information(
                self.iface.mainWindow(),
                'Копирование объектов',
                'Уже выполняется копирование — сначала завершите текущую проверку '
                '(закройте или подтвердите открытую форму атрибутов).',
            )
            return
        if self._pending_copy_list is not None or self._task is not None:
            QMessageBox.information(
                self.iface.mainWindow(),
                'Копирование объектов',
                'Список слоёв ещё загружается — подождите завершения.',
            )
            return

        copy_list = self._gather_layer_features()
        if not copy_list:
            QMessageBox.information(
                self.iface.mainWindow(),
                'Копирование объектов',
                'Нет объектов для копирования.\n'
                'Выделите один или несколько векторных слоёв в панели «Слои» '
                '(в них должны быть объекты) и повторите.',
            )
            return

        if len(copy_list) > self.LARGE_BATCH_WARNING:
            reply = QMessageBox.question(
                self.iface.mainWindow(),
                'Копирование объектов',
                'Объектов к копированию: {0}.\n'
                'Форма атрибутов будет открываться для каждого по очереди — '
                'это может занять много времени. Продолжить?'.format(len(copy_list)),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self._pending_copy_list = copy_list
        self._load_target_layer_options()

    def _load_target_layer_options(self):
        """Refreshes the Project+Database layer list off the main
        thread (matching the org's BufferPlugin), then shows the
        target-layer picker once it's ready. Credentials are resolved
        first and synchronously, since the prompt dialog needs the main
        thread; the potentially slow part (querying each DB connection)
        runs in the background."""
        if not self._connections:
            self._connections = get_connection_credentials()

        task = LongTaskLoader('Загрузка слоёв...', self._load_layers_info, self._connections)
        task.completed.connect(self._on_layers_loaded)
        task.terminated.connect(self._on_layers_load_failed)
        self._task = task

        self._progress_dialog = QProgressDialog(
            'Загрузка слоёв проекта и базы данных...', 'Отмена', 0, 100, self.iface.mainWindow(),
        )
        self._progress_dialog.setWindowModality(Qt.WindowModal)
        self._progress_dialog.setAutoClose(False)
        task.descriptionChanged.connect(self._progress_dialog.setLabelText)
        task.progressChanged.connect(self._progress_dialog.setValue)
        self._progress_dialog.canceled.connect(task.cancel)
        task.taskCompleted.connect(self._progress_dialog.close)
        task.taskTerminated.connect(self._progress_dialog.close)

        QgsApplication.taskManager().addTask(task)
        self._progress_dialog.show()

    def _load_layers_info(self, task, connections):
        """Runs off the main thread. Project layers are cheap and
        re-read every time; Database layers are queried only once per
        session and cached after that, so repeat runs feel instant."""
        proj_layers = get_project_layer_infos()
        task.progressChanged.emit(10)

        self._layers_info = [li for li in self._layers_info if li.connection_name != 'Проект']
        self._layers_info = proj_layers + self._layers_info
        if len(self._layers_info) == len(proj_layers):
            db_layers = get_db_layer_infos(task, connections)
            db_layers.sort(key=lambda li: (li.connection_name, li.schema, li.table_name))
            self._layers_info.extend(db_layers)

        task.progressChanged.emit(100)
        return True

    def _on_layers_loaded(self, result):
        self._task = None
        self._show_target_dialog()

    def _on_layers_load_failed(self, exception):
        self._task = None
        self._pending_copy_list = None
        if 'отмен' not in str(exception).lower():  # not a user-initiated cancel
            QMessageBox.critical(
                self.iface.mainWindow(),
                'Копирование объектов',
                'Не удалось получить список слоёв: {0}'.format(exception),
            )

    def _show_target_dialog(self):
        copy_list = self._pending_copy_list
        self._pending_copy_list = None
        if copy_list is None:
            return

        distinct_layers = {layer for layer, _ in copy_list}
        source_geom_types = {
            QgsWkbTypes.geometryType(feat.geometry().wkbType())
            for _, feat in copy_list if feat.geometry() and not feat.geometry().isEmpty()
        }

        dlg = TargetLayerDialog(
            self.iface.mainWindow(), len(copy_list), len(distinct_layers),
            self._layers_info, source_geom_types,
        )
        if dlg.exec_() != QDialog.Accepted:
            return

        info = dlg.selected_info()
        if info is None:
            QMessageBox.warning(self.iface.mainWindow(), 'Копирование объектов', 'Не выбран слой назначения.')
            return

        target_layer = resolve_layer(info)
        if target_layer is None:
            QMessageBox.critical(
                self.iface.mainWindow(),
                'Копирование объектов',
                'Не удалось открыть слой «{0}».'.format(info.display_name),
            )
            return

        # Features that happen to already live on the target layer itself
        # aren't copied anywhere -- there's nowhere else for them to go.
        filtered = [(layer, feat) for layer, feat in copy_list if layer is not target_layer]
        skipped_self = len(copy_list) - len(filtered)

        if not filtered:
            QMessageBox.information(
                self.iface.mainWindow(),
                'Копирование объектов',
                'Все выбранные объекты уже находятся на слое назначения — копировать нечего.',
            )
            return

        self._start_copy_review(filtered, target_layer, skipped_self)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _gather_layer_features(self):
        """Collect (layer, feature) pairs for every feature on every
        vector layer currently highlighted in the Layers panel -- the
        whole layer's content, not a feature-level selection within it.
        Features are copy-constructed so later edits to the source
        layers don't affect what's stored here."""
        copy_list = []
        for layer in self.iface.layerTreeView().selectedLayers():
            if not isinstance(layer, QgsVectorLayer):
                continue
            for feat in layer.getFeatures():
                copy_list.append((layer, QgsFeature(feat)))
        return copy_list

    def _build_feature(self, target_layer, target_fields, pk_names, source_layer, source_feat,
                        carry_over, transform_cache):
        """Build the new feature for the target layer: geometry copied (and
        reprojected if needed), attributes matched by field name. Any field
        currently in `carry_over` overrides that field's own source value.
        Returns (new_feature, own_original) where own_original holds what
        each field's value would be with NO carry-over applied, used later
        to detect which fields the operator actually touched."""
        geom = QgsGeometry(source_feat.geometry())
        if source_layer.crs() != target_layer.crs():
            key = source_layer.crs().authid() or source_layer.crs().toWkt()
            transform = transform_cache.get(key)
            if transform is None:
                transform = QgsCoordinateTransform(source_layer.crs(), target_layer.crs(), QgsProject.instance())
                transform_cache[key] = transform
            geom.transform(transform)

        # A source geometry with a Z value can't go into a target column
        # that doesn't have one -- PostGIS rejects the insert outright
        # rather than silently flattening it. Same clone -> dropZValue
        # pattern QGIS's own "Drop Z values" tool uses; the reverse case
        # (target wants Z, source doesn't have one) isn't handled here.
        if QgsWkbTypes.hasZ(geom.wkbType()) and not QgsWkbTypes.hasZ(target_layer.wkbType()):
            flattened = geom.constGet().clone()
            flattened.dropZValue()
            geom = QgsGeometry(flattened)

        new_feat = QgsFeature(target_fields)
        new_feat.setGeometry(geom)

        source_lookup = {f.name().lower(): f.name() for f in source_layer.fields()}
        own_original = {}
        for idx, field in enumerate(target_fields):
            name = field.name()
            if name in pk_names:
                continue
            src_name = source_lookup.get(name.lower())
            value = source_feat[src_name] if src_name else None
            own_original[name] = value
            new_feat.setAttribute(idx, carry_over.get(name, value))

        return new_feat, own_original

    def _abort_review(self):
        """Defensive cleanup for when a review is still in progress (a
        feature form is open) at unload time -- e.g. the plugin gets
        reloaded mid-review during development."""
        if self._current_dialog is not None:
            self._current_dialog.close()
            self._current_dialog = None
        if self._rubber_band is not None:
            self.iface.mapCanvas().scene().removeItem(self._rubber_band)
            self._rubber_band = None
        self._review = None

    def _start_copy_review(self, copy_list, target_layer, skipped_self=0):
        target_fields = target_layer.fields()
        pk_indexes = set(target_layer.dataProvider().pkAttributeIndexes())
        pk_names = {target_fields[i].name() for i in pk_indexes if 0 <= i < len(target_fields)}
        target_geom_type = QgsWkbTypes.geometryType(target_layer.wkbType())

        self._rubber_band = QgsRubberBand(self.iface.mapCanvas(), target_geom_type)
        self._rubber_band.setColor(QColor(255, 0, 255, 200))
        self._rubber_band.setWidth(3)

        self._review = {
            'queue': copy_list,
            'index': 0,
            'target_layer': target_layer,
            'target_fields': target_fields,
            'pk_names': pk_names,
            'target_geom_type': target_geom_type,
            'carry_over': {},
            'transform_cache': {},
            'done': 0,
            'skipped_geom': 0,
            'skipped_self': skipped_self,
            'stopped_early': False,
        }
        self._advance_review()

    def _advance_review(self):
        """Work through the queue until an item needs the operator's
        attention, open its form non-modally, then return -- the rest of
        the run continues from `_on_feature_form_finished` whenever that
        particular form closes, which may be right away or much later if
        the operator wanders off to look around the map first. Runs the
        closing summary once the queue is exhausted."""
        review = self._review
        queue = review['queue']
        target_layer = review['target_layer']

        while review['index'] < len(queue):
            source_layer, source_feat = queue[review['index']]
            review['index'] += 1

            source_geom = source_feat.geometry()
            if source_geom is None or source_geom.isEmpty():
                review['skipped_geom'] += 1
                continue
            if QgsWkbTypes.geometryType(source_geom.wkbType()) != review['target_geom_type']:
                review['skipped_geom'] += 1
                continue

            if not target_layer.isEditable() and not target_layer.startEditing():
                QMessageBox.critical(
                    self.iface.mainWindow(),
                    'Копирование объектов',
                    'Не удалось перевести слой назначения в режим редактирования.',
                )
                review['stopped_early'] = True
                self._finish_review()
                return

            new_feat, own_original = self._build_feature(
                target_layer, review['target_fields'], review['pk_names'],
                source_layer, source_feat, review['carry_over'], review['transform_cache'],
            )

            if not target_layer.addFeature(new_feat):
                review['skipped_geom'] += 1
                target_layer.rollBack()
                continue

            review['pending_fid'] = new_feat.id()
            review['pending_original'] = own_original

            self._highlight_and_frame(new_feat.geometry(), target_layer)

            # featureOwner=False: we keep our own reference (new_feat/fid)
            # rather than handing the feature's lifetime to the dialog.
            dlg = QgsAttributeDialog(target_layer, new_feat, False, self.iface.mainWindow())
            dlg.setWindowTitle('Атрибуты объекта')
            dlg.setAttribute(Qt.WA_DeleteOnClose)
            dlg.finished.connect(self._on_feature_form_finished)
            self._current_dialog = dlg
            dlg.show()  # non-blocking -- map navigation stays live
            return

        # Queue exhausted with nothing left needing a form.
        self._finish_review()

    def _highlight_and_frame(self, geometry, target_layer):
        """Highlight the object about to be edited. The view itself is
        only touched if the object doesn't already fit inside it -- an
        object bigger than the current view gets zoomed out to; one
        that's already fully visible is left exactly as the operator had
        it, so looking around between objects doesn't get undone."""
        canvas = self.iface.mapCanvas()
        self._rubber_band.setToGeometry(geometry, target_layer)

        if not canvas.extent().contains(geometry.boundingBox()):
            canvas.zoomToFeatureIds(target_layer, [self._review['pending_fid']])
        canvas.refresh()

    def _on_feature_form_finished(self, result):
        review = self._review
        target_layer = review['target_layer']
        self._current_dialog = None

        accepted = (result == QDialog.Accepted)

        if accepted:
            # Read the edited values back BEFORE committing: the
            # buffered feature's id is only valid while the edit session
            # is still open -- commit() replaces it with a permanent
            # one, so this has to happen first.
            final_feat = target_layer.getFeature(review['pending_fid'])
            own_original = review['pending_original']
            for field in review['target_fields']:
                name = field.name()
                if name in review['pk_names']:
                    continue
                new_val = final_feat[name]
                if new_val != own_original.get(name):
                    review['carry_over'][name] = new_val

        commit_ok = target_layer.commitChanges()

        if accepted and not commit_ok:
            # QGIS's own error dialog already shows the provider's
            # detailed message; roll back the failed buffered add so the
            # layer isn't left half-broken, and stop here rather than
            # opening more forms on top of a commit that isn't working.
            target_layer.rollBack()
            review['stopped_early'] = True
            review['commit_failed'] = True
            self._finish_review()
            return

        if accepted:
            review['done'] += 1
            self._advance_review()
        else:
            # Operator cancelled: stop here. The feature that was open
            # stays on the layer with its pre-filled values -- nothing
            # already confirmed is lost.
            review['stopped_early'] = True
            self._finish_review()

    def _finish_review(self):
        review = self._review
        self._review = None

        if self._rubber_band is not None:
            self.iface.mapCanvas().scene().removeItem(self._rubber_band)
            self._rubber_band = None

        total = len(review['queue'])
        message_lines = ['Скопировано и подтверждено объектов: {0} из {1}.'.format(review['done'], total)]
        if review['skipped_geom']:
            message_lines.append('Пропущено (пустая или несовместимая геометрия): {0}.'.format(review['skipped_geom']))
        if review['skipped_self']:
            message_lines.append('Пропущено (уже на слое назначения): {0}.'.format(review['skipped_self']))
        if review.get('commit_failed'):
            message_lines.append(
                'Сохранение объекта не удалось — обработка остановлена. '
                'Подробности см. в окне ошибки от QGIS.'
            )
        elif review['stopped_early'] and review['done'] < total:
            message_lines.append('Обработка остановлена пользователем.')

        QMessageBox.information(self.iface.mainWindow(), 'Копирование объектов', '\n'.join(message_lines))
