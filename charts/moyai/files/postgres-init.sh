#!/bin/sh
set -eu
# psql variables quote generated values as SQL literals, never as SQL source.
psql --username "$POSTGRES_USER" --dbname postgres --set ON_ERROR_STOP=1 <<'SQL'
\getenv moyai_password MOYAI_DB_PASSWORD
\getenv temporal_password TEMPORAL_PASSWORD
CREATE ROLE moyai LOGIN PASSWORD :'moyai_password';
CREATE ROLE temporal LOGIN PASSWORD :'temporal_password';
CREATE DATABASE moyai OWNER moyai;
CREATE DATABASE temporal OWNER temporal;
CREATE DATABASE temporal_visibility OWNER temporal;
REVOKE CONNECT ON DATABASE moyai FROM PUBLIC;
REVOKE CONNECT ON DATABASE temporal FROM PUBLIC;
REVOKE CONNECT ON DATABASE temporal_visibility FROM PUBLIC;
SQL
