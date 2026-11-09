# -*- coding: utf-8 -*-
"""
QGIS / NextGIS launch logger.

Sends one "application started" event to the internal LogDesktopService each
time QGIS or NextGIS QGIS starts. Standard library only, runs in a background
thread, and never raises: if anything goes wrong, QGIS starts normally and at
most an Info line appears under View > Panels > Log Messages > "LaunchLog".

Deployment (use ONE hook, not both):

  * startup.py (recommended)
    Copy this file, keeping the name startup.py, into a system-wide QGIS data
    directory (admin/root-owned). QGIS executes EVERY startup.py it finds in
    QStandardPaths.AppDataLocation, so a user's own startup.py runs alongside
    this one rather than replacing it. To list the directories for a given
    build, run this in that build's Python console:

        from qgis.PyQt.QtCore import QStandardPaths
        QStandardPaths.standardLocations(QStandardPaths.AppDataLocation)

    On Windows pick the C:/ProgramData/... entry (survives upgrades);
    on Linux pick the /usr/share/... entry.

  * PYQGIS_STARTUP
    Point the machine-level PYQGIS_STARTUP variable at this file.

A guard prevents double logging if both hooks happen to be present.
Works with QGIS 3.x (Python 3.6+) on Windows and Linux.
"""

import getpass
import os
import socket
import sys
import threading
import urllib.parse
import urllib.request

# --- Settings ---------------------------------------------------------------

# Route name is misspelled on the server side; keep it as is.
LOG_URL = "http://sng-gis-web.sgc.oil.gas/LogDesktopService/addLogNoAutoriztion"
ACTION_LAUNCH = 3          # event code the service uses for "application started"
DOMAIN = "SGC"             # prefix for bare user names; set to "" to send them as is
TIMEOUT_S = 5              # network timeout; runs in the background either way
LOG_TAG = "LaunchLog"      # tab name in QGIS's Log Messages panel (failures only)

# The old module also forced the Oracle client locale for the whole QGIS
# process (decimal point instead of comma, UTF-8 client charset). Kept so
# Oracle layers behave as before; set to False once you've confirmed they
# don't need it.
FORCE_ORACLE_NLS_LANG = True
ORACLE_NLS_LANG = "American_America.AL32UTF8"

_GUARD = "_sgc_launch_log_started"


# --- Helpers ----------------------------------------------------------------

def _app_name():
    """Return 'nextgis' for NextGIS QGIS and 'qgis' for anything else."""
    from qgis.PyQt.QtCore import QCoreApplication

    hints = " ".join((
        QCoreApplication.applicationName(),
        QCoreApplication.organizationName(),
        QCoreApplication.applicationFilePath(),
    )).lower()
    return "nextgis" if ("nextgis" in hints or "ngqgis" in hints) else "qgis"


def _user_name():
    """Logon name, prefixed with DOMAIN unless it is already qualified."""
    if sys.platform == "win32":
        try:
            user = os.getlogin()       # GetUserNameW: the real logon name
        except OSError:
            user = getpass.getuser()
    else:
        user = getpass.getuser()       # os.getlogin() can fail for GUI launches
    if DOMAIN and "\\" not in user and "@" not in user:
        user = DOMAIN + "\\" + user
    return user


def _local_ip(host, port):
    """IP of the network interface that routes to the log server.

    Connecting a UDP socket only selects a route; no packet is sent.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, port))
            return s.getsockname()[0]
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return ""


def _note(message):
    """Best-effort Info entry in QGIS's Log Messages panel. Never raises."""
    try:
        from qgis.core import Qgis, QgsMessageLog

        level = getattr(Qgis, "Info", None)
        if level is None:              # QGIS 4: scoped enums only
            level = Qgis.MessageLevel.Info
        QgsMessageLog.logMessage(message, LOG_TAG, level)
    except Exception:
        pass


def _send(app, user):
    """Runs in a background thread: one GET, errors noted and swallowed."""
    try:
        parts = urllib.parse.urlsplit(LOG_URL)
        query = urllib.parse.urlencode({
            "appName": app,
            "actionId": ACTION_LAUNCH,
            "userIP": _local_ip(parts.hostname, parts.port or 80),
            "userName": user,
        })
        # Direct connection: the service is on the intranet, so bypass any
        # system proxy (same as the old httplib2 code did).
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(LOG_URL + "?" + query, timeout=TIMEOUT_S) as resp:
            resp.read()
    except Exception as exc:
        _note("Launch event not sent: {}".format(exc))


# --- Entry point ------------------------------------------------------------

def main():
    if getattr(sys, _GUARD, False):    # already ran in this QGIS session
        return
    setattr(sys, _GUARD, True)

    if FORCE_ORACLE_NLS_LANG:
        os.environ["NLS_LANG"] = ORACLE_NLS_LANG

    try:
        app, user = _app_name(), _user_name()
    except Exception as exc:
        _note("Launch event not sent: {}".format(exc))
        return

    threading.Thread(
        target=_send, args=(app, user), name="LaunchLog", daemon=True
    ).start()


try:
    main()
except Exception:
    pass
