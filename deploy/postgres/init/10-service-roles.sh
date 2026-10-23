#!/bin/sh
# Роли БД для сервисов (ADR-019): у каждого сервиса своя роль и своя схема с тем же именем.
# Выполняется entrypoint'ом postgres только при инициализации пустого тома.
set -e

create_service_role() {
    role="$1"
    password="$2"
    if [ -z "$password" ]; then
        echo "service role $role: password is not set, skipped"
        return 0
    fi
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "${POSTGRES_DB:-$POSTGRES_USER}" \
        -v role="$role" -v password="$password" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'password') \gexec
SELECT format('CREATE SCHEMA IF NOT EXISTS %I AUTHORIZATION %I', :'role', :'role') \gexec
SELECT format('ALTER ROLE %I SET search_path = %I', :'role', :'role') \gexec
SQL
}

create_service_role harness "${HARNESS_DB_PASSWORD:-}"
create_service_role metabrain "${METABRAIN_DB_PASSWORD:-}"
