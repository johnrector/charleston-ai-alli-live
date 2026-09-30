"""Match one expiring server-owned call authorization without expanding global access."""
import json
from datetime import datetime, timezone


def match_single_call(candidate, raw_grant, now=None):
    """Return only the administrator's exact call body, or fail closed.

    Never trust caller-supplied capabilities, logistics or request IDs. A matched
    grant replaces them with one approved body/ID, so the existing durable claim
    permits at most one dial even if callers vary incidental request fields.
    No credentials or persistent access grants are involved.
    """
    if not raw_grant:
        return None
    try:
        grant = json.loads(raw_grant)
        if not isinstance(grant, dict) or set(grant) != {'expires_at', 'call'}:
            return None
        expiry = datetime.fromisoformat(grant['expires_at'].replace('Z', '+00:00'))
        clock = now or datetime.now(timezone.utc)
        if expiry.tzinfo is None or clock.tzinfo is None or expiry <= clock:
            return None
        approved = grant['call']
        if not isinstance(approved, dict) or not approved.get('request_id'):
            return None
        fields = ('phone', 'recipient_name', 'email', 'mission')
        if any(not isinstance(approved.get(field), str) or not approved[field] for field in fields):
            return None
        if any(candidate.get(field) != approved[field] for field in fields):
            return None
        return approved
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
