-- Vuelve a la base que guardó `restore.sh --replace-existing-data` (runbook §6). Solo borra `crm` si la
-- base guardada existe; si no, falla sin tocar nada. Con el runtime parado:
--   docker compose … exec -T postgres psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
--     -v kept=crm_replaced_<…> < rollback-replaced.sql
SELECT count(*) = 1 AS kept_exists FROM pg_database
  WHERE datname = :'kept' AND datname LIKE 'crm\_replaced\_%' \gset
\if :kept_exists
DROP DATABASE IF EXISTS crm WITH (FORCE);
ALTER DATABASE :"kept" RENAME TO crm;
ALTER DATABASE crm WITH ALLOW_CONNECTIONS true;
\else
DO $$ BEGIN RAISE EXCEPTION 'la base guardada no existe: crm no se toca'; END $$;
\endif
