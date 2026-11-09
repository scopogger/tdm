# NextGIS launch logger — Windows test rollout

Nov 5, 2026 · @Dean Fest

On one test PC: remove the old QgisLog plugin, put `startup.py` in an admin-only NextGIS folder, then check that launches get logged and that a normal user can't remove it. Plan on about 30 minutes.

**Before you start**

- Admin rights on a test PC with NextGIS QGIS LTR 3.44 installed.
- `startup.py` saved somewhere on that PC.
- A second, non-admin account on the same PC (for step 5).
- Someone who can see LogDesktopService's records, to confirm a row arrives.
- Run PowerShell commands **as administrator** unless a step says otherwise.

## Step 1 — Find where NextGIS looks for startup.py

NextGIS decides these folders itself, so read them from NextGIS instead of guessing.

1. Start NextGIS and open **Plugins → Python Console**.
2. Run these two lines:

```python
from qgis.PyQt.QtCore import QStandardPaths, QCoreApplication
print("\n".join(QStandardPaths.standardLocations(QStandardPaths.AppDataLocation))); print(QCoreApplication.applicationName(), "|", QCoreApplication.organizationName(), "|", QCoreApplication.applicationFilePath())
```

3. From the folder list, copy the line that starts with `C:/ProgramData/`. This is your **target folder** (`$target` below). It usually doesn't exist yet; that's fine.
4. Check that the last printed line contains `nextgis` or `ngqgis` somewhere, in any case. That's how the script tells NextGIS from plain QGIS. If neither appears, send me the line: launches would be logged as `qgis` instead of `nextgis`.

If the list has no `C:/ProgramData/` line, use the one under `C:/Program Files/NextGIS/` instead. It's also admin-only, but a NextGIS reinstall or upgrade can wipe it.

## Step 2 — Remove the old logger

The old module can be wired in two ways, and both must go or it keeps throwing the `httplib2` error. Remove the variable first, then the folders it points into.

**2a. The old PYQGIS\_STARTUP variable**

```powershell
'Machine','User' | ForEach-Object { '{0}: {1}' -f $_, [Environment]::GetEnvironmentVariable('PYQGIS_STARTUP', $_) }
```

If the Machine value points at a `QgisLog\__init__.py`, remove it:

```powershell
[Environment]::SetEnvironmentVariable('PYQGIS_STARTUP', $null, 'Machine')
```

If it points at anything else, leave it: something else uses it. The User line only shows the account running PowerShell. If a user has their own copy, run the same command with `'User'` in a normal (non-admin) PowerShell signed in as that user.

**2b. The old plugin folders**

This lists every plugin folder that contains the old module, for all users and all QGIS installs on the PC:

```powershell
$old = Get-Item -Force -ErrorAction SilentlyContinue -Path @(
  'C:\Users\*\AppData\Roaming\NextGIS\QGIS\3\LTR\profiles\*\python\plugins\*',
  'C:\Users\*\AppData\Roaming\QGIS\QGIS3\profiles\*\python\plugins\*',
  'C:\Program Files\NextGIS\QGIS LTR\share\qgis\python\plugins\*',
  'C:\Program Files\QGIS*\apps\qgis*\python\plugins\*'
) | Where-Object { $_.PSIsContainer -and (Test-Path (Join-Path $_.FullName 'qgis_log.py')) }
$old.FullName
```

Expect folders named `Log` or `QgisLog`. Once the list looks right, delete them (this is permanent):

```powershell
$old | Remove-Item -Recurse -Force
```

**2c. Sign out and back in** so new programs pick up the variable change.

If the folders or the variable come back after a reboot or the next sign-in, a logon script or Group Policy is re-creating them. Whoever set up the old rollout needs to switch that off too.

## Step 3 — Install startup.py

Copy the file into the target folder and make it read-only for users. Fill in the first two lines, then run all five:

```powershell
$target = 'C:/ProgramData/...'        # the line you copied in step 1
$src    = 'C:\path\to\startup.py'     # where you saved the file
New-Item -ItemType Directory -Path $target -Force | Out-Null
Copy-Item -Path $src -Destination (Join-Path $target 'startup.py') -Force
icacls (Join-Path $target 'startup.py') /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' '*S-1-5-32-545:RX'
```

- The file name must stay exactly `startup.py`.
- `icacls` leaves SYSTEM and Administrators with full control and Users with read-only. It uses group SIDs, not names, so it works on Russian Windows, where the groups are «Администраторы» and «Пользователи».
- It should report 1 file processed and 0 failed. To check the result, run `icacls (Join-Path $target 'startup.py')`: you should see exactly three entries, SYSTEM `(F)`, Administrators `(F)`, Users `(RX)`.

## Step 4 — Check that launches get logged

1. Start NextGIS from the Start menu as usual. No error dialog should appear, and startup should take as long as before.
2. In the Python Console, run:

```python
import sys, os; print(getattr(sys, "_sgc_launch_log_started", False), os.environ.get("NLS_LANG"))
```

Expected: `True American_America.AL32UTF8`. `False` means NextGIS didn't run the file: recheck the folder against step 1's list and the file name.

3. Wait about 10 seconds, then open **View → Panels → Log Messages**. No **LaunchLog** tab means no failure was recorded. If the tab is there, its message says why the request failed.
4. Ask whoever has access to LogDesktopService to find a row from the last few minutes with `appName=nextgis`, your `SGC\username` and this PC's IP.

## Step 5 — Check a normal user can't remove or bypass it

Sign in with the non-admin account for all three checks.

**5a. The file can't be deleted or edited.** Paste the target folder into Explorer's address bar (ProgramData is hidden). Try to delete `startup.py`, then try to edit and save it in Notepad. Both should be refused with an access-denied or "You need permission" message.

**5b. There's nothing to switch off.** In NextGIS, open **Plugins → Manage and Install Plugins**. No logging plugin should be listed.

**5c. A user's own startup.py doesn't replace it.** Run step 1's command in NextGIS under this account and take the first folder in the list (the one under `C:/Users/<this account>/AppData/Roaming/`). Create a `startup.py` there containing:

```python
import sys; sys._user_startup_ran = True
```

Restart NextGIS and run in the Python Console:

```python
import sys; print(getattr(sys, "_user_startup_ran", False), getattr(sys, "_sgc_launch_log_started", False))
```

Expected: `True True`, meaning both files ran. This launch should also show up as a server row. Delete the test file afterwards.

## Step 6 — Check Oracle layers and decide on NLS\_LANG

The script forces `NLS_LANG=American_America.AL32UTF8` for the whole NextGIS process, as the old plugin did, because Oracle layers may depend on it. This step checks whether they do.

1. With the script as installed, open an Oracle layer that has Cyrillic text and decimal numbers. Check that the attribute table shows Cyrillic correctly and the numbers are right. If you have a test layer you can edit, save one change too.
2. As admin, open `startup.py` in the target folder, change `FORCE_ORACLE_NLS_LANG = True` to `False` and save. Restart NextGIS and repeat 1.
3. If anything breaks without it (`???` instead of Cyrillic, `ORA-01722: invalid number`, wrong decimals), set it back to `True`. If nothing changes, leave it `False`.

With the flag off, the console check from step 4 shows the PC's own NLS\_LANG value, or `None`.

## If something goes wrong

| What you see | Likely cause | What to do |
| --- | --- | --- |
| Error dialog at startup mentioning `httplib2` | An old copy is still being run | Redo step 2: both variable scopes, and plugin folders for every user |
| Console check prints `False` | Wrong folder or file name | Compare the folder with step 1's list exactly; the name must be `startup.py` |
| LaunchLog: `timed out` or `getaddrinfo failed` | The PC can't reach the log server | Run `Test-NetConnection sng-gis-web.sgc.oil.gas -Port 80` |
| LaunchLog: `HTTP Error 4xx` or `5xx` | The server rejected the request | Send me the message |

The script connects to the log server directly, skipping any system proxy, because the service is internal. If your network only reaches it through a proxy, tell me.

**To undo:** as admin, delete `startup.py` from the target folder. Logging stops at the next NextGIS start.

**Plain QGIS still on some PCs:** it uses different folders. Run step 1 in that QGIS and copy the same file into its `C:/ProgramData/…` line, typically `C:/ProgramData/QGIS/QGIS3`. Those launches log as `appName=qgis`.

## What to send back

- [ ] Step 1 output: the folder list and the last line (app name | organization | path)
- [ ] Step 2: which leftovers it found (PYQGIS\_STARTUP values, plugin folders), and whether any came back after a reboot
- [ ] Step 4: the console line, whether a LaunchLog tab appeared (and its message), whether the server row arrived
- [ ] Step 5: the result of 5a, 5b and 5c
- [ ] Step 6: Oracle with the flag on and off

Once the path and results are in, I'll turn steps 2 and 3 into one PowerShell script for the mass rollout.
