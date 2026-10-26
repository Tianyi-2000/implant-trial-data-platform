# ADR-008: Closing a data query

Status: accepted, 2026-10-25. Requirements: REQ-04, REQ-12.

## Context

A re-export carries a different threshold for one electrode, with the same `measured_at` as the stored one. Someone has to decide which value is right. The options were:

- overwrite automatically, so the latest file wins;
- accept automatically by measurement time;
- raise a data query, and let a person decide.

## Decision

- **A load never overwrites.** Every conflict raises a data query that keeps two things:
  - the stored row as the query saw it, in `stored`;
  - the resent values, in `proposed`.
- **Only a data manager closes a query, and only through `ops.resolve_query(query, decision, reason)`.** Nobody has UPDATE on `ops.data_query`. The decision is one of:
  - `accept_resent` (conflicts only) applies the proposed values, and points the row at the batch they came from;
  - `keep_stored` changes no data.
- **The change and the closed query are one transaction.** The function adds `DQ-<id> <decision>:` in front of the audit reason, and both audit rows share one `xact_id`. So the query leads to the change, and the change leads back to the query.
- **`resolved_by` is `session_user`.** Inside this `SECURITY DEFINER` function, `current_user` is the function's owner, not the person.
- **A stale decision is refused.** Accepting checks that the stored row still holds what the query showed. If someone corrected it, or another query was accepted first, accepting would silently undo that change, so the data manager has to review it again.
- **Two people closing the same query are serialized.** `FOR UPDATE` on the query makes the second wait, and then it finds the query already closed.

## Consequences

- A person has to act on every conflict. Conflicts should be rare. Many of them point to an upstream problem worth investigating.
- The full query workflow, open → answered by the site → closed by data management, is not built. The `answered` status exists in the schema for it.
- Refused attempts are not in the audit trail, because they were rolled back. In production they would be logged separately, through the server log or pgaudit.

## Alternatives considered

- **Overwrite automatically.** Rejected:
  - the same `measured_at` with a different value means history was rewritten, not that the electrode was measured again;
  - the audit trail would only show the overwrite after the fact, while a query stops it beforehand;
  - it would collide with locked visits (ADR-009).
- **Accept automatically by time.** If the sites confirm that a later `measured_at` always means a re-test, a later value could be accepted automatically, and only "same time, different value" would raise a query. This is the first change to make once that practice is confirmed.
