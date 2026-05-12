#!/bin/bash
# Скрипт удаления Редактор PDF Альт
# sudo bash uninstall_altpdf.sh

set -e

if [ "$EUID" -ne 0 ]; then
    echo "Ошибка: запустите с правами root"
    exit 1
fi

echo "=== Удаление AltPDF ==="

rm -rf /opt/AltPDF
rm -f /usr/share/applications/altpdf.desktop
rm -f /usr/share/mime/packages/altpdf.xml
rm -f /usr/local/bin/altpdf
rm -f /usr/share/pixmaps/altpdf.png

for SIZE in 16 32 48 64 128 256; do
    rm -f "/usr/share/icons/hicolor/${SIZE}x${SIZE}/apps/altpdf.png"
done

# Убираем запись из mimeapps.list
MIMEAPPS_SYSTEM="/usr/share/applications/mimeapps.list"
[ -f "$MIMEAPPS_SYSTEM" ] && \
    sed -i '/^application\/pdf=altpdf.desktop/d' "$MIMEAPPS_SYSTEM" || true

update-desktop-database /usr/share/applications 2>/dev/null || true
update-mime-database /usr/share/mime           2>/dev/null || true
gtk-update-icon-cache -f -t /usr/share/icons/hicolor 2>/dev/null || true

echo "=== Удаление завершено ==="
