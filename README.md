# MetaBrain — установка

MetaBrain даёт каждому человеку в компании цифровых сотрудников: двойника его роли и сотрудников на роли, которых у него нет. Они разбирают закупки и заявки, проверяют данные и документы, присылают отчёты с источниками. Сотрудники советуют, решает человек.

Здесь только то, что нужно для установки: установщики и описание сервисов. Сам MetaBrain приходит готовым образом `ghcr.io/graycep1-spec/metabrain`.

## Что нужно

- **Docker.** Linux-сервер: Docker Engine и Compose v2. Windows 10/11 и macOS: Docker Desktop.
- **Свободное место:** около 1 ГБ на сервере, 2 ГБ на Windows.
- **Ключ DeepSeek API** — модель, которой думают сотрудники.
- **Отдельный бот в Telegram** из BotFather — через него приходят отчёты и выполняется вход. Не берите бота, который уже работает в другом сервисе.
- Для входа в кабинет через сайт Telegram — **домен с HTTPS** и Client ID/Secret из BotFather → бот → Login Widget. Без домена входите через бота.

## Установка

Linux-сервер:

```bash
git clone https://github.com/graycep1-spec/metabrain-agi.git
cd metabrain-agi
sudo ./install.sh
```

С HTTPS для кабинета (nginx и certbot уже стоят, A-запись домена указывает на сервер):

```bash
sudo ./install.sh --domain cab.example.ru --email admin@example.ru
```

macOS — то же без `sudo` и без `--domain`: MetaBrain ставится в `~/metabrain`, кабинет на `http://localhost:8081`.

Windows: скачайте репозиторий (Code → Download ZIP), распакуйте и в PowerShell из этой папки выполните

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Установщик скачивает образ, создаёт ключи и пароли, скрыто спрашивает ключ DeepSeek и токен бота, поднимает сервисы и проверяет ключи. Ключи хранятся только в `deploy/.env` на вашей машине.

## После установки

1. Откройте кабинет: адрес установщик напишет в конце. На своей машине — `http://localhost:8081`.
2. Войдите: «Войти через Telegram» (нужен домен с HTTPS) или «Войти через бота».
3. Главная показывает следующий шаг: профиль организации → сценарий → запуск команды → данные («Попробовать на примере», если своих нет) → «Запустить сейчас». Через минуту — первый отчёт с источниками.

## Команды

| Что | Linux / macOS | Windows |
|---|---|---|
| Обновить до свежего образа | `git pull && sudo ./install.sh update` | `.\install.ps1 -Action update` |
| Поменять ключи | `sudo ./install.sh configure` | `.\install.ps1 -Action configure` |
| Состояние сервисов | `sudo ./install.sh status` | `.\install.ps1 -Action status` |
| Остановить (данные остаются) | `sudo ./install.sh uninstall` | `.\install.ps1 -Action uninstall` |
| Удалить всё, включая данные | `sudo ./install.sh uninstall --purge` | `.\install.ps1 -Action uninstall -Purge` |

Конкретная версия вместо последней: `METABRAIN_IMAGE=ghcr.io/graycep1-spec/metabrain:v0.1.0 sudo -E ./install.sh`, в PowerShell — `$env:METABRAIN_IMAGE='ghcr.io/graycep1-spec/metabrain:v0.1.0'` перед запуском.

## Что ставится

Семь сервисов в своём проекте Docker Compose `metabrain` (`deploy/compose.yml`): кабинет, харнесс, валидатор, мета-мозг и его MCP, учебный ландшафт и Postgres. Своя сеть и свой том с данными; наружу открыт только кабинет на `127.0.0.1:8081`. Чужие контейнеры, образы и сайты nginx установщик не трогает, поэтому MetaBrain можно ставить рядом с другими сервисами.

Повторный запуск установщика — обновление: настройки (`deploy/.env`) и данные (том `metabrain_pgdata`) сохраняются.

## Если что-то не так

| Симптом | Что сделать |
|---|---|
| «не удалось скачать образ» | проверьте доступ сервера к `ghcr.io` |
| «мало места на диске» | освободите место: нужно около 1 ГБ |
| Сервис не поднялся | `docker compose -f /opt/metabrain/deploy/compose.yml logs <сервис>` |
| Бот молчит | `configure`: проверьте ключ DeepSeek и токен бота; у бота не должно быть вебхука |
| Вход через Telegram не открывается | нужен домен с HTTPS и Allowed URL `https://<домен>/auth/callback` в Login Widget; или входите через бота |

## Лицензия

GPL-3.0, см. `LICENSE`.
