"""Auth — multi-user identity and per-user isolation.

The system is multi-tenant: every project (and everything reachable from it) is
owned by a user via `owner_id`, and all queries are owner-scoped so users only
ever see their own data. Built on fastapi-users (see auth.users).
"""
