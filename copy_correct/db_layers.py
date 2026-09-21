"""
Enumerates candidate target layers for Copy & Correct: layers already
in the current project, plus tables from any PostgreSQL connection
configured in QGIS (queried directly, without adding them to the
project) -- so the operator can copy into a layer that isn't currently
loaded, not just one already sitting in the Layers panel.

This is adapted from the org's existing BufferPlugin, which does the
same kind of Project+Database layer picking and is already in
production use -- same QSettings connection lookup, same
QgsCredentials prompt-on-demand, same geometry_columns query, same
on-demand QgsVectorLayer construction for the chosen table, run off
the main thread via the same QgsTask wrapper. Differences from that
original:
  - the Project group is labelled in Russian ("Проект") rather than
    left as the literal English string;
  - PostGIS geometry-type strings are matched more completely (POINT/
    LINESTRING/POLYGON and their MULTI* variants), rather than only a
    few exact strings, since this drives the geometry-compatibility
    check in the picker dialog;
  - an empty connections list returns no database layers instead of
    raising, so a Project-only copy still works with zero PostgreSQL
    connections configured.
"""

from dataclasses import dataclass

from qgis.PyQt.QtCore import QSettings, pyqtSignal
from qgis.PyQt.QtSql import QSqlDatabase, QSqlQuery

from qgis.core import (
    Qgis,
    QgsCredentials,
    QgsDataSourceUri,
    QgsMessageLog,
    QgsProject,
    QgsTask,
    QgsVectorLayer,
    QgsWkbTypes,
)

PROJECT_GROUP = 'Проект'


@dataclass
class PostgisLayerInfo:
    connection_name: str
    schema: str
    table_name: str
    geometry_column: str
    primary_key: str
    geometry_type: int  # a QgsWkbTypes.GeometryType value
    display_name: str
    uri: QgsDataSourceUri


class LongTaskLoader(QgsTask):
    """Generic background task: runs `long_function(self, *args,
    **kwargs)` off the main thread and reports back via signals."""

    progressChanged = pyqtSignal(int)
    completed = pyqtSignal(object)
    terminated = pyqtSignal(Exception)
    descriptionChanged = pyqtSignal(str)

    def __init__(self, description, long_function, *args, **kwargs):
        super().__init__(description, QgsTask.CanCancel)
        self.long_function = long_function
        self.exception = None
        self._cancelled = False
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            result = self.long_function(self, *self.args, **self.kwargs)
            self.completed.emit(result)
            return result if result is not None else True
        except Exception as e:
            self.exception = e
            self.terminated.emit(e)
            return False

    def cancel(self):
        self._cancelled = True
        super().cancel()
        self.terminated.emit(Exception('Пользователь отменил задачу'))

    def isCancelled(self):
        return self._cancelled

    def setDescription(self, new_desc):
        super().setDescription(new_desc)
        self.descriptionChanged.emit(new_desc)


def get_postgis_credentials(host, port, dbname, user=''):
    """Prompts for missing DB credentials via QGIS's own credentials
    dialog -- the same one QGIS's native DB connections use, so it
    respects the user's master password store if they have one set up."""
    connection_info = "dbname='{0}' host={1} port={2}".format(dbname, host, port)
    success, user_cred, pass_cred = QgsCredentials.instance().get(connection_info, user, None)
    if not success:
        return '', '', False
    return user_cred, pass_cred, True


def get_connection_credentials():
    """Reads every PostgreSQL connection configured in QGIS, filling in
    any missing username/password by prompting once and caching the
    result back into QSettings (the same store QGIS's own connection
    manager uses) so later calls don't ask again."""
    connections = []
    settings = QSettings()
    settings.beginGroup('/PostgreSQL/connections/')
    for conn_name in settings.childGroups():
        settings.beginGroup(conn_name)
        host = settings.value('host', '')
        port = settings.value('port', '5432')
        dbname = settings.value('database', '')
        user = settings.value('username', '')
        password = settings.value('password', '')

        if not user or not password:
            user_c, pass_c, ok = get_postgis_credentials(host, port, dbname, user)
            if not ok:
                settings.endGroup()
                continue
            settings.setValue('username', user_c)
            settings.setValue('password', pass_c)
            settings.sync()
            user, password = user_c, pass_c

        connections.append({
            'conn_name': conn_name, 'host': host, 'port': port,
            'dbname': dbname, 'user': user, 'password': password,
        })
        settings.endGroup()
    settings.endGroup()
    return connections


def _wkb_geometry_type_from_pg_type(pg_type):
    t = (pg_type or '').upper()
    if 'POINT' in t:
        return QgsWkbTypes.PointGeometry
    if 'LINE' in t:
        return QgsWkbTypes.LineGeometry
    if 'POLYGON' in t:
        return QgsWkbTypes.PolygonGeometry
    return QgsWkbTypes.UnknownGeometry


def get_project_layer_infos():
    """Layers already loaded in the project -- read straight off the
    live layer, no DB round-trip needed. Works for any vector provider,
    not just PostGIS: `resolve_layer` only needs the layer's name to
    find it again, never the schema/table/uri stored here."""
    infos = []
    for lyr in QgsProject.instance().mapLayers().values():
        if not isinstance(lyr, QgsVectorLayer) or not lyr.isValid():
            continue
        if lyr.geometryType() not in (
            QgsWkbTypes.PointGeometry, QgsWkbTypes.LineGeometry, QgsWkbTypes.PolygonGeometry,
        ):
            continue
        uri = QgsDataSourceUri(lyr.source())
        infos.append(PostgisLayerInfo(
            connection_name=PROJECT_GROUP,
            schema=uri.schema(),
            table_name=uri.table(),
            geometry_column=uri.geometryColumn(),
            primary_key=uri.keyColumn() or 'mslink',
            geometry_type=lyr.geometryType(),
            display_name=lyr.name(),
            uri=uri,
        ))
    return infos


def get_db_layer_infos(task, connections):
    """Queries geometry_columns on each configured connection for its
    spatial tables, without loading any of them as project layers."""
    layers_info = []
    if not connections:
        return layers_info

    total = len(connections)
    step = 90 / total if total else 0
    progress = 10

    for conn in connections:
        if task.isCancelled():
            raise Exception('Задача отменена')
        task.setDescription('Загрузка слоёв из подключения {0}'.format(conn['conn_name']))

        try:
            host, port, dbname = conn['host'], conn['port'], conn['dbname']
            user, password = conn['user'], conn['password']
            if not all([host, dbname]):
                continue

            db_alias = 'copy_correct_{0}'.format(conn['conn_name'])
            db = QSqlDatabase.addDatabase('QPSQL', db_alias)
            db.setHostName(host)
            db.setPort(int(port))
            db.setDatabaseName(dbname)
            db.setUserName(user)
            db.setPassword(password)

            if not db.open():
                QgsMessageLog.logMessage(
                    'Не удалось подключиться к {0}: {1}'.format(conn['conn_name'], db.lastError().text()),
                    'CopyCorrect', Qgis.Warning,
                )
                QSqlDatabase.removeDatabase(db_alias)
                continue

            query = QSqlQuery(db)
            query.exec_('SELECT f_table_schema, f_table_name, f_geometry_column, type FROM geometry_columns')

            while query.next():
                if task.isCancelled():
                    raise Exception('Задача отменена')
                schema = query.value(0)
                table = query.value(1)
                geom_col = query.value(2)
                geom_type_db = query.value(3)

                if geom_type_db == 'GEOMETRY':
                    continue

                uri = QgsDataSourceUri()
                uri.setConnection(host, port, dbname, user, password)
                uri.setDataSource(schema, table, geom_col, '', 'mslink')

                layers_info.append(PostgisLayerInfo(
                    connection_name=conn['conn_name'],
                    schema=schema,
                    table_name=table,
                    geometry_column=geom_col,
                    primary_key='mslink',
                    geometry_type=_wkb_geometry_type_from_pg_type(geom_type_db),
                    display_name='{0}.{1}'.format(schema, table),
                    uri=uri,
                ))

            db.close()
            QSqlDatabase.removeDatabase(db_alias)

        except Exception as e:
            QgsMessageLog.logMessage('Ошибка в {0}: {1}'.format(conn['conn_name'], e), 'CopyCorrect', Qgis.Critical)
        finally:
            progress += step
            task.progressChanged.emit(int(progress))

    return layers_info


def resolve_layer(info):
    """Turns a chosen PostgisLayerInfo into a live QgsVectorLayer: the
    existing project layer for Project-group entries (matched by
    name), or a fresh layer built straight from the DB URI for
    everything else -- not added to the project, per the original
    plugin's "don't clutter the working set" behaviour."""
    if info.connection_name == PROJECT_GROUP:
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.name() == info.display_name:
                return lyr
        return None

    layer = QgsVectorLayer(info.uri.uri(), info.display_name, 'postgres')
    return layer if layer.isValid() else None
