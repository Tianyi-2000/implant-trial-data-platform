-- Cluster-wide roles. Run once per PostgreSQL cluster, as the bootstrap superuser.
-- Demo passwords equal the role name: local demo only.
--
-- Owners (NOLOGIN; nobody works as them day to day):
--   audit_owner     owns the audit schema; the only role that writes audit rows, through the trigger
--   platform_owner  owns ops, ref and core; runs schema migrations
-- Groups (NOLOGIN) hold privileges; people and services log in as members.
-- service_account marks non-human logins, which must say who or what each change is for.

CREATE ROLE audit_owner     NOLOGIN;
CREATE ROLE platform_owner  NOLOGIN;

CREATE ROLE service_account NOLOGIN;
CREATE ROLE loader          NOLOGIN;
CREATE ROLE data_manager    NOLOGIN;
CREATE ROLE analyst         NOLOGIN;
CREATE ROLE auditor         NOLOGIN;
CREATE ROLE transformer     NOLOGIN;

CREATE ROLE loader_svc     LOGIN PASSWORD 'loader_svc'     IN ROLE loader, service_account;
CREATE ROLE dbt_svc        LOGIN PASSWORD 'dbt_svc'        IN ROLE transformer, service_account;
CREATE ROLE data_manager_1 LOGIN PASSWORD 'data_manager_1' IN ROLE data_manager;
CREATE ROLE analyst_1      LOGIN PASSWORD 'analyst_1'      IN ROLE analyst;
CREATE ROLE auditor_1      LOGIN PASSWORD 'auditor_1'      IN ROLE auditor;
