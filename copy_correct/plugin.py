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
     Its attribute form opens automatically so the operator can review
     and correct it; the feature is highlighted on the map for as long
     as its form is open.
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
from qgis.gui import QgsMapLayerComboBox, QgsRubberBand


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

    def initGui(self):
        icon = QgsApplication.getThemeIcon('mActionEditCopy.svg')
        self.action = QAction(icon, 'Копировать с проверкой атрибутов', self.iface.mainWindow())
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu(self.MENU_NAME, self.action)
        self.iface.addToolBarIcon(self.action)

    def unload(self):
        self.iface.removePluginMenu(self.MENU_NAME, self.action)
        self.iface.removeToolBarIcon(self.action)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self):
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

        self._run_copy_review(filtered, target_layer, skipped_self)

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

    def _run_copy_review(self, copy_list, target_layer, skipped_self=0):
        canvas = self.iface.mapCanvas()
        target_fields = target_layer.fields()
        pk_indexes = set(target_layer.dataProvider().pkAttributeIndexes())
        pk_names = {target_fields[i].name() for i in pk_indexes if 0 <= i < len(target_fields)}
        target_geom_type = QgsWkbTypes.geometryType(target_layer.wkbType())

        rb = QgsRubberBand(canvas, target_geom_type)
        rb.setColor(QColor(255, 0, 255, 200))
        rb.setWidth(3)

        carry_over = {}
        transform_cache = {}
        done = 0
        skipped_geom = 0
        stopped_early = False

        try:
            for source_layer, source_feat in copy_list:
                source_geom = source_feat.geometry()
                if source_geom is None or source_geom.isEmpty():
                    skipped_geom += 1
                    continue
                if QgsWkbTypes.geometryType(source_geom.wkbType()) != target_geom_type:
                    skipped_geom += 1
                    continue

                if not target_layer.isEditable() and not target_layer.startEditing():
                    QMessageBox.critical(
                        self.iface.mainWindow(),
                        'Копирование объектов',
                        'Не удалось перевести слой назначения в режим редактирования.',
                    )
                    stopped_early = True
                    break

                new_feat, own_original = self._build_feature(
                    target_layer, target_fields, pk_names, source_layer, source_feat,
                    carry_over, transform_cache,
                )

                if not target_layer.addFeature(new_feat):
                    skipped_geom += 1
                    target_layer.rollBack()
                    continue

                # Highlight + centre the map on the copy the operator is
                # about to edit, for the duration of its form.
                rb.setToGeometry(new_feat.geometry(), target_layer)
                canvas.setCenter(new_feat.geometry().centroid().asPoint())
                canvas.refresh()

                accepted = self.iface.openFeatureForm(target_layer, new_feat)
                target_layer.commitChanges()

                if accepted:
                    done += 1
                    for field in target_fields:
                        name = field.name()
                        if name in pk_names:
                            continue
                        new_val = new_feat[name]
                        if new_val != own_original.get(name):
                            carry_over[name] = new_val
                else:
                    # Operator cancelled: stop here. The feature that was
                    # open stays on the layer with its pre-filled values --
                    # nothing already confirmed is lost.
                    stopped_early = True
                    break
        finally:
            rb.reset(target_geom_type)

        total = len(copy_list)
        message_lines = ['Скопировано и подтверждено объектов: {0} из {1}.'.format(done, total)]
        if skipped_geom:
            message_lines.append('Пропущено (пустая или несовместимая геометрия): {0}.'.format(skipped_geom))
        if skipped_self:
            message_lines.append('Пропущено (уже на слое назначения): {0}.'.format(skipped_self))
        if stopped_early and done < total:
            message_lines.append('Обработка остановлена пользователем.')

        QMessageBox.information(self.iface.mainWindow(), 'Копирование объектов', '\n'.join(message_lines))
