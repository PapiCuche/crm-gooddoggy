#!/bin/sh
# Roles y BD de producción (ADR-002 §1, ADR-033 §3): la variante de infra/docker/postgres/init/01-roles.sh
# sin CREATEDB del migrador ni BD de tests. Idempotente y repetible: crea lo que falta y fija los
# atributos y las contraseñas. Las contraseñas llegan por el entorno y psql las lee con \getenv: ni
# este archivo ni los argumentos de un proceso las contienen.
#   - Primer arranque: la imagen lo ejecuta desde docker-entrypoint-initdb.d con postgres.env.
#   - Restauración (ADR-033 §8.1), antes del volcado y con contraseñas nuevas:
#       sudo docker compose … exec -T -e CRM_MIGRATOR_PASSWORD -e CRM_APP_PASSWORD postgres \
#         /docker-entrypoint-initdb.d/10-roles.sh
#   crm_migrator: propietario del esquema, ejecuta migraciones; BYPASSRLS, sin CREATEDB.
#   crm_app:      runtime; NO propietario, NO superusuario, NO BYPASSRLS; solo DML.
set -eu
: "${CRM_MIGRATOR_PASSWORD:?falta CRM_MIGRATOR_PASSWORD}" "${CRM_APP_PASSWORD:?falta CRM_APP_PASSWORD}"
export CRM_DB_NAME="${CRM_DB_NAME:-crm}"

psql -v ON_ERROR_STOP=1 -q --username "${POSTGRES_USER:-postgres}" --dbname postgres <<'EOSQL'
\getenv migrator_pw CRM_MIGRATOR_PASSWORD
\getenv app_pw CRM_APP_PASSWORD
\getenv db CRM_DB_NAME
SELECT 'CREATE ROLE crm_migrator' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'crm_migrator') \gexec
SELECT 'CREATE ROLE crm_app' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'crm_app') \gexec
ALTER ROLE crm_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION BYPASSRLS PASSWORD :'migrator_pw';
ALTER ROLE crm_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'app_pw';
SELECT format('CREATE DATABASE %I OWNER crm_migrator', :'db')
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db') \gexec
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"db" TO crm_app;

\connect :"db"

ALTER SCHEMA public OWNER TO crm_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO crm_app;
-- Todo objeto que cree crm_migrator será accesible (solo DML) por crm_app.
ALTER DEFAULT PRIVILEGES FOR ROLE crm_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crm_app;
ALTER DEFAULT PRIVILEGES FOR ROLE crm_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO crm_app;
-- Cada función (p. ej., SECURITY DEFINER) concede EXECUTE a crm_app de forma explícita (ADR-002 §3.3).
ALTER DEFAULT PRIVILEGES FOR ROLE crm_migrator
  REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
EOSQL
