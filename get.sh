#!/usr/bin/env bash
# Загрузчик MetaBrain для Linux и macOS: скачать релиз → сверить SHA-256 → распаковать → установщик.
# Со страницы лендинга: curl -fsSL https://<страница>/get.sh | bash
# Явно: METABRAIN_BASE=https://<страница> METABRAIN_VERSION=v0.1.0 bash get.sh
#
# Скрипт ничего не устанавливает молча: он проверяет подпись архива и либо запускает установщик (если это
# терминал), либо печатает точную команду запуска. Ключи у вас спросит уже установщик.
set -euo pipefail

# BASE подставляет процесс сборки релиза; до публикации — переменная METABRAIN_BASE (OPEN-18).
BASE_DEFAULT="__METABRAIN_BASE__"
BASE="${METABRAIN_BASE:-$BASE_DEFAULT}"
VERSION="${METABRAIN_VERSION:-latest}"

die() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "нужна команда $1"; }

case "$BASE" in
  https://*) : ;;
  *) die "адрес дистрибутивов не задан — укажите METABRAIN_BASE=https://<страница> (лендинг ещё не опубликован, OPEN-18)" ;;
esac

OS=$(uname -s)
case "$OS" in
  Linux|Darwin) EXT=tar.gz ;;
  *) die "эта оболочка — для Linux и macOS; на Windows используйте get.ps1 в PowerShell" ;;
esac

need curl; need tar
if command -v sha256sum >/dev/null 2>&1; then SHA="sha256sum"; else need shasum; SHA="shasum -a 256"; fi

if [ "$VERSION" = "latest" ]; then
  VERSION=$(curl -fsSL "$BASE/latest.txt" | tr -d ' \r\n') || die "не удалось узнать последнюю версию ($BASE/latest.txt)"
  [ -n "$VERSION" ] || die "пустой latest.txt"
fi

ARCHIVE="metabrain-$VERSION.$EXT"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "Скачиваю $ARCHIVE …"
curl -fsSL "$BASE/$ARCHIVE" -o "$TMP/$ARCHIVE" || die "не скачался $BASE/$ARCHIVE"
curl -fsSL "$BASE/SHA256SUMS" -o "$TMP/SHA256SUMS" || die "не скачался $BASE/SHA256SUMS"

expected=$(awk -v f="$ARCHIVE" '$2 == f || $2 == "*"f {print $1}' "$TMP/SHA256SUMS" | head -1)
[ -n "$expected" ] || die "в SHA256SUMS нет строки для $ARCHIVE"
actual=$($SHA "$TMP/$ARCHIVE" | awk '{print $1}')
[ "$expected" = "$actual" ] || die "контрольная сумма не совпала — архив повреждён или подменён, установку прервал"
echo "SHA-256 совпал."

DEST="${METABRAIN_DIR:-$TMP/metabrain-$VERSION}"
mkdir -p "$DEST"
tar -xzf "$TMP/$ARCHIVE" -C "$DEST" --strip-components=1 2>/dev/null || tar -xzf "$TMP/$ARCHIVE" -C "$DEST"
[ -f "$DEST/install.sh" ] || die "в архиве нет install.sh"
chmod +x "$DEST/install.sh"

if [ -t 0 ]; then
  echo "Запускаю установщик…"
  trap - EXIT   # оставляем распакованные файлы установщику
  exec "$DEST/install.sh" "$@"
else
  trap - EXIT
  echo
  echo "Скачано и проверено: $DEST"
  if [ "$OS" = "Darwin" ]; then
    echo "Запустите:  cd \"$DEST\" && ./install.sh"
  else
    echo "Запустите:  cd \"$DEST\" && sudo ./install.sh"
  fi
fi
