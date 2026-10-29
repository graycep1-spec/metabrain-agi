#!/usr/bin/env bash
# Установка MetaBrain на Linux. Нужен только Docker (Engine + Compose v2). Повторный запуск — обновление:
# конфигурация (deploy/.env) и данные (том metabrain_pgdata) сохраняются.
#
#   sudo ./install.sh                 установить или обновить, спросить ключи, которых нет
#   sudo ./install.sh configure       поменять ключи: DeepSeek, бот, адрес кабинета, вход через Telegram
#   sudo ./install.sh status          что запущено и какие ключи заданы (без значений)
#   sudo ./install.sh tokens          токены менеджеров демо-ландшафта — их вводят в кабинете
#   sudo ./install.sh uninstall       остановить (данные остаются); --purge — удалить всё
#
# Флаги: --dir DIR (по умолчанию /opt/metabrain)  --yes (без вопросов)  --web-port PORT (8081)
#        --deepseek-key-from FILE (взять DEEPSEEK_API_KEY из env-файла другого сервиса, не показывая его)
#        --domain DOMAIN --email EMAIL (HTTPS: отдельный сайт nginx + Let's Encrypt)
#
# Рядом с другими сервисами безопасно: свой проект compose, своя сеть и база, наружу только кабинет на
# 127.0.0.1; nginx трогается только с --domain и только своим файлом; чужие контейнеры и образы не удаляются.
set -euo pipefail

OS=$(uname -s)
DIR=""        # по умолчанию: /opt/metabrain на Linux, ~/metabrain на macOS (Docker Desktop монтирует только дом)
ACTION=install
YES=0
PURGE=0
KEY_FROM=""
DOMAIN=""
EMAIL=""
WEB_PORT=""

say() { printf '\n== %s\n' "$*"; }
die() { printf '\nОшибка: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    install|update|configure|status|tokens|uninstall) ACTION=$1 ;;
    --dir) DIR=$2; shift ;;
    --yes|-y) YES=1 ;;
    --purge) PURGE=1 ;;
    --deepseek-key-from) KEY_FROM=$2; shift ;;
    --domain) DOMAIN=$2; shift ;;
    --email) EMAIL=$2; shift ;;
    --web-port) WEB_PORT=$2; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) die "неизвестный аргумент: $1 (см. --help)" ;;
  esac
  shift
done

if [ -z "$DIR" ]; then
  if [ "$OS" = "Darwin" ]; then DIR="$HOME/metabrain"; else DIR=/opt/metabrain; fi
fi
if [ "$OS" = "Darwin" ] && [ -n "$DOMAIN" ]; then
  die "--domain (HTTPS через nginx) — только для Linux-сервера; на macOS кабинет работает на http://localhost"
fi

SRC=$(cd "$(dirname "$0")" && pwd)
# Образ: рядом исходники (Dockerfile.stub) — собрать; иначе — готовый образ из реестра (публичная установка).
# Свой образ или версия: METABRAIN_IMAGE=ghcr.io/graycep1-spec/metabrain:v0.1.0 sudo -E ./install.sh
if [ -f "$SRC/Dockerfile.stub" ]; then
  IMAGE="${METABRAIN_IMAGE:-metabrain/app:local}"
else
  IMAGE="${METABRAIN_IMAGE:-ghcr.io/graycep1-spec/metabrain:latest}"
fi
export METABRAIN_IMAGE="$IMAGE"   # deploy/compose.yml берёт образ отсюда
ENV_FILE="$DIR/deploy/.env"
INTERACTIVE=0
if [ "$YES" -eq 0 ] && [ -t 0 ]; then INTERACTIVE=1; fi

compose() { docker compose -f "$DIR/deploy/compose.yml" --env-file "$ENV_FILE" "$@"; }
# Модуль установщика в образе MetaBrain: секреты идут через stdin и не печатаются.
installer() { docker run --rm -i --user 0 -v "$DIR:/w" -w /w "$IMAGE" python -m src.common.installer "$@"; }

preflight() {
  command -v docker >/dev/null 2>&1 || die "нужен Docker Engine: https://docs.docker.com/engine/install/"
  docker compose version >/dev/null 2>&1 || die "нужен Docker Compose v2 (команда docker compose)"
  docker info >/dev/null 2>&1 || die "Docker не запущен или нет прав — запустите через sudo"
  local parent need=700 free
  parent=$(dirname "$DIR")
  mkdir -p "$parent"
  # обновление пересобирает только слой с кодом; первая установка — образ целиком и, возможно, postgres:16
  docker image inspect "$IMAGE" >/dev/null 2>&1 && need=200
  docker image inspect postgres:16 >/dev/null 2>&1 || need=$((need + 650))
  free=$(df -Pm "$parent" | awk 'NR==2 {print $4}')
  [ "$free" -ge "$need" ] || die "мало места на диске: свободно ${free} МБ, нужно не меньше ${need} МБ"
  echo "Docker $(docker version --format '{{.Server.Version}}'), свободно ${free} МБ"
}

sync_code() {
  [ "$SRC" = "$DIR" ] && return 0
  say "Код → $DIR"
  mkdir -p "$DIR"
  rm -rf "$DIR/src"   # без устаревших модулей прошлой версии
  tar -C "$SRC" --exclude=.git --exclude=./deploy/.env --exclude=./deploy/tsn/.env -cf - . | tar -C "$DIR" -xf -
  # Установка 22.10 на сервере tsn жила в deploy/tsn — переносим её конфигурацию, проект и данные те же.
  if [ ! -f "$ENV_FILE" ] && [ -f "$DIR/deploy/tsn/.env" ]; then
    mv "$DIR/deploy/tsn/.env" "$ENV_FILE"
    echo "Конфигурация перенесена из deploy/tsn/.env"
  fi
  rm -f "$DIR/deploy/tsn/compose.yml"   # прежний compose установки 22.10 — теперь deploy/compose.yml
}

fetch_image() {
  OLD_IMAGE=$(docker image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null || true)
  if [ -f "$DIR/Dockerfile.stub" ]; then
    say "Сборка образа (около минуты)"
    docker build -q -t "$IMAGE" -f "$DIR/Dockerfile.stub" "$DIR" >/dev/null
  else
    say "Загрузка образа $IMAGE"
    docker pull -q "$IMAGE" >/dev/null || die "не удалось скачать образ $IMAGE — проверьте доступ в интернет"
  fi
}

drop_old_image() {
  local new
  new=$(docker image inspect -f '{{.Id}}' "$IMAGE")
  if [ -n "${OLD_IMAGE:-}" ] && [ "$OLD_IMAGE" != "$new" ]; then
    docker image rm "$OLD_IMAGE" >/dev/null 2>&1 && echo "Старый образ MetaBrain удалён" || true
  fi
}

ask_secret() {  # имя, вопрос
  local value
  read -r -s -p "$2 (Enter — оставить как есть): " value; echo
  [ -n "$value" ] && printf '%s\n' "$value" | installer set "$1" --env deploy/.env
  return 0
}

ask_plain() {  # имя, вопрос
  local value
  read -r -p "$2 (Enter — оставить как есть): " value
  [ -n "$value" ] && printf '%s\n' "$value" | installer set "$1" --env deploy/.env
  return 0
}

configure() {
  say "Конфигурация"
  installer init --env deploy/.env --template .env.example
  if [ -n "$KEY_FROM" ]; then
    [ -r "$KEY_FROM" ] || die "--deepseek-key-from: нет файла $KEY_FROM"
    docker run --rm -i --user 0 -v "$DIR:/w" -v "$(cd "$(dirname "$KEY_FROM")" && pwd)/$(basename "$KEY_FROM"):/import.env:ro" \
      -w /w "$IMAGE" python -m src.common.installer import DEEPSEEK_API_KEY --from /import.env --env deploy/.env
  fi
  printf '%s\n' "$IMAGE" | installer set METABRAIN_IMAGE --env deploy/.env >/dev/null
  [ -n "$WEB_PORT" ] && printf '%s\n' "$WEB_PORT" | installer set WEB_PORT --env deploy/.env
  [ -n "$DOMAIN" ] && printf 'https://%s\n' "$DOMAIN" | installer set WEB_BASE_URL --env deploy/.env
  installer show --env deploy/.env
  if [ "$INTERACTIVE" -eq 1 ]; then
    echo
    echo "Ключи вводятся скрыто и сохраняются только в $ENV_FILE (права 600)."
    ask_secret DEEPSEEK_API_KEY "Ключ DeepSeek API"
    ask_secret TELEGRAM_BOT_TOKEN "Токен ОТДЕЛЬНОГО бота MetaBrain из BotFather (не бота другого сервиса)"
    [ -z "$DOMAIN" ] && ask_plain WEB_BASE_URL "Адрес кабинета с https, например https://metabrain.example.ru"
    ask_plain TELEGRAM_OIDC_CLIENT_ID "Client ID входа через Telegram (BotFather → Login Widget)"
    ask_secret TELEGRAM_OIDC_CLIENT_SECRET "Client Secret входа через Telegram"
  fi
  chmod 600 "$ENV_FILE"
}

wait_healthy() {
  local pending="" i
  for i in $(seq 1 40); do
    pending=$(compose ps --format '{{.Service}} {{.Health}}' | awk '$2 != "healthy" {print $1}')
    [ -z "$pending" ] && break
    sleep 3
  done
  compose ps --format 'table {{.Service}}\t{{.Status}}'
  [ -z "$pending" ] || die "не поднялись: $(echo "$pending" | tr '\n' ' ')— docker compose -f $DIR/deploy/compose.yml logs"
}

up() {
  say "Запуск"
  compose up -d --remove-orphans
  wait_healthy
  drop_old_image
}

checks() {
  say "Проверки"
  installer check-llm --env deploy/.env || true
  installer check-bot --env deploy/.env || true
  # username бота мог записаться проверкой — перечитываем конфигурацию
  compose up -d harness web >/dev/null 2>&1 || true
}

setup_https() {
  [ -n "$DOMAIN" ] || return 0
  say "HTTPS для $DOMAIN"
  command -v nginx >/dev/null 2>&1 || die "--domain: нужен nginx"
  command -v certbot >/dev/null 2>&1 || die "--domain: нужен certbot (apt install certbot python3-certbot-nginx)"
  [ -n "$EMAIL" ] || die "--domain: укажите --email для Let's Encrypt"
  [ -d /etc/nginx/sites-available ] || die "--domain: ожидается /etc/nginx/sites-available"
  local site="/etc/nginx/sites-available/metabrain-$DOMAIN" link="/etc/nginx/sites-enabled/metabrain-$DOMAIN"
  local port="${WEB_PORT:-8081}"
  if [ ! -f "$site" ]; then
    cat > "$site" <<NGINX
# MetaBrain: кабинет $DOMAIN → 127.0.0.1:$port (создано install.sh; другие сайты не затрагиваются)
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 25m;   # выгрузки закупок и документация; по умолчанию nginx режет на 1 МБ (413)
    location / {
        proxy_pass http://127.0.0.1:$port;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    }
}
NGINX
    ln -sf "$site" "$link"
  fi
  if ! nginx -t >/dev/null 2>&1; then
    rm -f "$link" "$site"
    die "nginx -t не прошёл с новым сайтом — сайт удалён, остальные не тронуты"
  fi
  systemctl reload nginx
  certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --non-interactive --redirect
  compose up -d harness web
}

summary() {
  local url run="sudo $DIR/install.sh"
  [ "$OS" = "Darwin" ] && run="$DIR/install.sh"
  url=$(installer show --env deploy/.env | awk -F': ' '$1 == "WEB_BASE_URL" {print $2}')
  say "Готово"
  echo "Кабинет: $url   (проверка: curl http://127.0.0.1:${WEB_PORT:-8081}/healthz)"
  echo "Токены менеджеров демо:   $run tokens"
  echo "Поменять ключи:           $run configure"
  case "$url" in
    https://*) echo "В BotFather → бот → Login Widget добавьте Allowed URL: $url/auth/callback" ;;
    *) if [ "$OS" = "Darwin" ]; then
         echo "Вход через Telegram требует адреса с https — на рабочей станции его нет; для демо со входом ставьте на сервер."
       else
         echo "Для входа в кабинет нужен адрес с https: $run --domain ДОМЕН --email ПОЧТА"
       fi ;;
  esac
}

case "$ACTION" in
  install|update)
    [ "$(id -u)" -eq 0 ] || [ -w "$(dirname "$DIR")" ] || die "запустите через sudo"
    preflight
    sync_code
    fetch_image
    configure
    up
    setup_https
    checks
    summary
    ;;
  configure)
    configure
    compose up -d
    wait_healthy
    setup_https
    checks
    summary
    ;;
  status)
    compose ps --format 'table {{.Service}}\t{{.Status}}'
    installer show --env deploy/.env
    ;;
  tokens)
    installer tokens --env deploy/.env
    ;;
  uninstall)
    if [ "$INTERACTIVE" -eq 1 ]; then
      read -r -p "Остановить MetaBrain$([ "$PURGE" -eq 1 ] && echo ' и удалить данные и конфигурацию') ? [y/N] " answer
      [ "$answer" = "y" ] || exit 0
    fi
    if [ "$PURGE" -eq 1 ]; then
      compose down -v --remove-orphans
      docker image rm "$IMAGE" >/dev/null 2>&1 || true
      for link in /etc/nginx/sites-enabled/metabrain-*; do
        [ -e "$link" ] || continue
        rm -f "$link" "/etc/nginx/sites-available/$(basename "$link")"
        nginx -t >/dev/null 2>&1 && systemctl reload nginx
      done
      rm -rf "$DIR"
      echo "MetaBrain удалён полностью"
    else
      compose down --remove-orphans
      echo "MetaBrain остановлен; данные и конфигурация в $DIR остались"
    fi
    ;;
esac
