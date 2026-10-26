# ADR-002: Who made the change

Status: accepted, 2026-10-24

## Context

Every change to a clinical record has to be attributable (ALCOA+; 21 CFR Part 11 §11.10(e)). The first draft took the user name from a session setting, `SET LOCAL app.user_id = '...'`. Any connected session can set that to anyone's name, so it is a claim, not an identity.

## Decision

- **Each demo person and service has their own database login**: `loader_svc`, `dbt_svc`, `data_manager_1`, `analyst_1`, `auditor_1`. Shared logins are not allowed.
- The audit trail records two things:
  - `db_user` = `session_user`. This is the login the database itself authenticated, and it can't be spoofed from inside the session.
  - `app_user` = `app.user_id`, the person or job a service account says it acts for.
- Service accounts must set `app.user_id`, or the change is refused. Superusers count too, because `pg_has_role` treats them as members of every role. So an admin fixing data by hand must also say who it is for, like a break-glass ticket.
- Updates and deletes require `app.change_reason`.

## Consequences

- In a real web application, many users share one connection pool login. There, `app_user` is only as trustworthy as the application's own authentication. This is a known limitation, stated in [DETAILS.md](../DETAILS.md#known-limitations).
- A superuser can change `session_user` with `SET SESSION AUTHORIZATION`. Superuser access needs its own controls: see ADR-003.

## Alternatives considered

- Only `app.user_id`. Simple, but anyone can claim any name.
- One database login per real person in production. This gives the strongest attribution, but it means managing database accounts for every site user. More realistic is SSO into the application, which then sets `app_user`.
