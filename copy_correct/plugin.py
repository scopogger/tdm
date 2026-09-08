"""
Copy & Correct: copy selected features from one or more layers into an
existing target layer, reviewing and correcting attributes one feature
at a time.

Workflow:
  1. Select features on one or more layers (QGIS keeps selection
     per-layer; this tool gathers everything currently selected across
     every vector layer in the project).
  2. Pick the existing layer to copy into.
  3. Each selected feature is copied into the target layer in turn.
     Its attribute form opens automatically, non-modally, so the
     operator can still pan/zoom the map freely while it's open. The
     feature is highlighted on the map for as long as its form is
     open; the view is only re-framed if the feature doesn't already
     fit inside it -- an object bigger than the current view gets
     zoomed out to, one that's already visible is left exactly as the
     operator had it.
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
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAction,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from qgis.core import (
    QgsApplication,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsMapLayerProxyModel,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsAttributeDialog, QgsMapLayerComboBox, QgsRubberBand


class TargetLayerDialog(QDialog):
    """Small confirmation dialog: shows what's selected, picks the target layer."""

    def __init__(self, parent, feature_count, layer_count):
        super().__init__(parent)
        self.setWindowTitle('Копирование объектов')

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            'Выбрано объектов: {0} (слоёв: {1})'.format(feature_count, layer_count)
        ))
        layout.addWidget(QLabel('Слой назначения:'))

        self.layer_combo = QgsMapLayerComboBox(self)
        self.layer_combo.setFilters(QgsMapLayerProxyModel.VectorLayer)
        layout.addWidget(self.layer_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Ok).setText('Начать')
        buttons.button(QDialogButtonBox.Cancel).setText('Отмена')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_layer(self):
        return self.layer_combo.currentLayer()


class CopyCorrectPlugin:
    """Plugin entry point: registers the toolbar/menu action."""

    MENU_NAME = '&Копирование с проверкой'

    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self._current_dialog = None
        self._rubber_band = None
        self._review = None  # holds state for the in-progress copy run, if any

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

        copy_list = self._gather_selected_features()
        if not copy_list:
            QMessageBox.information(
                self.iface.mainWindow(),
                'Копирование объектов',
                'Не выбрано ни одного объекта.\n'
                'Выделите объекты на одном или нескольких слоях и повторите.',
            )
            return

        distinct_layers = {layer for layer, _ in copy_list}
        dlg = TargetLayerDialog(self.iface.mainWindow(), len(copy_list), len(distinct_layers))
        if dlg.exec_() != QDialog.Accepted:
            return

        target_layer = dlg.selected_layer()
        if target_layer is None:
            QMessageBox.warning(self.iface.mainWindow(), 'Копирование объектов', 'Не выбран слой назначения.')
            return

        # Features that happen to already live on the target layer itself
        # aren't copied anywhere -- there's nowhere else for them to go.
        filtered = [(layer, feat) for layer, feat in copy_list if layer is not target_layer]
        skipped_self = len(copy_list) - len(filtered)

        for layer in distinct_layers:
            layer.removeSelection()

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

    def _gather_selected_features(self):
        """Collect (layer, feature) pairs for every selected feature on every
        vector layer in the project. Features are copy-constructed so later
        clearing the live selection doesn't affect what's stored here."""
        copy_list = []
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or layer.selectedFeatureCount() == 0:
                continue
            for feat in layer.selectedFeatures():
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
            canvas.zoomToFeatureIds(target_layer, {self._review['pending_fid']})
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

        target_layer.commitChanges()

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
        if review['stopped_early'] and review['done'] < total:
            message_lines.append('Обработка остановлена пользователем.')

        QMessageBox.information(self.iface.mainWindow(), 'Копирование объектов', '\n'.join(message_lines))
