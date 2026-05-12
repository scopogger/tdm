#!/bin/bash
# =============================================================================
# Скрипт установки Редактор PDF Альт на Альт Рабочая станция
# Запускать с правами root: sudo bash install_altpdf.sh
# =============================================================================

set -e

APP_NAME="AltPDF"
APP_DIR="/opt/AltPDF"
DESKTOP_DIR="/usr/share/applications"
ICON_DIR_BASE="/usr/share/icons/hicolor"
MIME_DIR="/usr/share/mime/packages"
BINARY="$APP_DIR/AltPDF"

# Определяем директорию откуда запущен скрипт
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Установка $APP_NAME ==="

# ── 1. Проверка прав ──────────────────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    echo "Ошибка: запустите скрипт с правами root (sudo bash install_altpdf.sh)"
    exit 1
fi

# ── 2. Создание директории приложения ─────────────────────────────────────────
echo "→ Установка файлов в $APP_DIR ..."
mkdir -p "$APP_DIR"
cp -r "$SCRIPT_DIR"/. "$APP_DIR"/
chmod +x "$BINARY"

# ── 3. Установка иконок ───────────────────────────────────────────────────────
echo "→ Установка иконок ..."

# Пробуем конвертировать .ico в .png через ImageMagick
# Если convert недоступен — просто копируем .ico (DE обычно умеет .ico)
ICO_SRC="$APP_DIR/icons/icon.ico"

install_icon() {
    local SIZE=$1
    local DEST="$ICON_DIR_BASE/${SIZE}x${SIZE}/apps"
    mkdir -p "$DEST"
    if command -v convert >/dev/null 2>&1 && [ -f "$ICO_SRC" ]; then
        # extract the closest size frame from the .ico
        convert "${ICO_SRC}[0]" -resize "${SIZE}x${SIZE}" \
            "$DEST/altpdf.png" 2>/dev/null || \
        convert "$ICO_SRC" -resize "${SIZE}x${SIZE}" \
            "$DEST/altpdf.png" 2>/dev/null || true
    fi
    # Fallback: copy the .ico itself (works in most GTK DEs)
    if [ ! -f "$DEST/altpdf.png" ] && [ -f "$ICO_SRC" ]; then
        cp "$ICO_SRC" "$DEST/altpdf.png"
    fi
}

for SIZE in 16 32 48 64 128 256; do
    install_icon $SIZE
done

# Также кладём в pixmaps для legacy-совместимости (некоторые меню Alt берут оттуда)
mkdir -p /usr/share/pixmaps
if command -v convert >/dev/null 2>&1 && [ -f "$ICO_SRC" ]; then
    convert "${ICO_SRC}[0]" -resize "48x48" /usr/share/pixmaps/altpdf.png 2>/dev/null || \
    cp "$ICO_SRC" /usr/share/pixmaps/altpdf.png 2>/dev/null || true
else
    [ -f "$ICO_SRC" ] && cp "$ICO_SRC" /usr/share/pixmaps/altpdf.png || true
fi

echo "→ Обновление кэша иконок ..."
gtk-update-icon-cache -f -t "$ICON_DIR_BASE" 2>/dev/null || true

# ── 4. Установка .desktop файла ───────────────────────────────────────────────
echo "→ Установка .desktop файла ..."
cat > "$DESKTOP_DIR/altpdf.desktop" << 'DESKTOP'
[Desktop Entry]
Version=1.0
Type=Application
Name=Редактор PDF Альт
Name[ru]=Редактор PDF Альт
Name[en]=AltPDF Editor
GenericName=Просмотрщик PDF
GenericName[ru]=Просмотрщик PDF
GenericName[en]=PDF Viewer
Comment=Просмотр и редактирование PDF-документов
Comment[ru]=Просмотр и редактирование PDF-документов
Comment[en]=View and edit PDF documents
Exec=/opt/AltPDF/AltPDF %F
Icon=altpdf
MimeType=application/pdf;
Categories=Office;Viewer;Graphics;
StartupNotify=true
StartupWMClass=AltPDF
Keywords=PDF;документ;редактор;просмотр;
Keywords[en]=PDF;document;editor;viewer;
DESKTOP

chmod 644 "$DESKTOP_DIR/altpdf.desktop"

echo "→ Обновление базы .desktop файлов ..."
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

# ── 5. Регистрация MIME-типа ──────────────────────────────────────────────────
echo "→ Регистрация MIME-типа application/pdf ..."
mkdir -p "$MIME_DIR"
cat > "$MIME_DIR/altpdf.xml" << 'MIME'
<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="application/pdf">
    <comment>PDF документ</comment>
    <comment xml:lang="ru">PDF документ</comment>
    <glob pattern="*.pdf"/>
    <glob pattern="*.PDF"/>
    <magic priority="50">
      <match type="string" offset="0" value="%PDF-"/>
    </magic>
  </mime-type>
</mime-info>
MIME

echo "→ Обновление базы MIME-типов ..."
update-mime-database /usr/share/mime 2>/dev/null || true

# ── 6. Установка как приложения по умолчанию для PDF ─────────────────────────
echo "→ Установка AltPDF как приложения по умолчанию для PDF ..."

# Системный дефолт (для всех пользователей)
if command -v xdg-mime >/dev/null 2>&1; then
    xdg-mime default altpdf.desktop application/pdf 2>/dev/null || true
fi

# Через mimeapps.list (надёжнее на ALT Linux)
MIMEAPPS_SYSTEM="/usr/share/applications/mimeapps.list"
if [ -f "$MIMEAPPS_SYSTEM" ]; then
    # Удалим старую запись если есть, добавим новую
    sed -i '/^application\/pdf=/d' "$MIMEAPPS_SYSTEM" 2>/dev/null || true
    # Проверяем есть ли секция [Default Applications]
    if grep -q "^\[Default Applications\]" "$MIMEAPPS_SYSTEM"; then
        sed -i '/^\[Default Applications\]/a application\/pdf=altpdf.desktop' \
            "$MIMEAPPS_SYSTEM"
    else
        printf '\n[Default Applications]\napplication/pdf=altpdf.desktop\n' \
            >> "$MIMEAPPS_SYSTEM"
    fi
else
    printf '[Default Applications]\napplication/pdf=altpdf.desktop\n' \
        > "$MIMEAPPS_SYSTEM"
fi

# ── 7. Символическая ссылка в /usr/local/bin для запуска из консоли ───────────
echo "→ Создание символической ссылки /usr/local/bin/altpdf ..."
ln -sf "$BINARY" /usr/local/bin/altpdf

# ── 8. Итог ───────────────────────────────────────────────────────────────────
echo ""
echo "=== Установка завершена ==="
echo ""
echo "  Приложение: $BINARY"
echo "  Ярлык меню: $DESKTOP_DIR/altpdf.desktop"
echo "  Запуск из консоли: altpdf [файл.pdf]"
echo "  Открыть PDF двойным кликом: да (приложение по умолчанию)"
echo ""
echo "  Если иконка не появилась в меню — выйдите и снова войдите в систему,"
echo "  или выполните: sudo gtk-update-icon-cache -f -t /usr/share/icons/hicolor"
echo ""
