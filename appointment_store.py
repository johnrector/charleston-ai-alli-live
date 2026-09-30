"""Durable, fail-closed dispatch ledger for appointment confirmation calls.

A key is claimed exactly once, regardless of its eventual dispatch status. A
worker crash after claiming therefore needs manual reconciliation, never an
automatic redial. Contact context must be minimized and bounded by the caller.
No database connections are opened at import time.
"""

from threading import Lock
from typing import Any, Literal

from psycopg.types.json import Jsonb

from google_store import connection


_initialized = False
_initialize_lock = Lock()
_DISPOSITION_RANK = {
    'queued': 0,
    'ringing': 1,
    'in-progress': 2,
    'completed': 3,
    'busy': 3,
    'failed': 3,
    'no-answer': 3,
    'canceled': 3,
}


def initialize() -> None:
    """Create the ledger once per process, serializing first-run DDL workers."""
    global _initialized
    if _initialized:
        return
    with _initialize_lock:
        if _initialized:
            return
        with connection() as db:
            # Different from google_store's action-lock key. This transaction
            # lock also protects simultaneous first starts in other processes.
            db.execute('SELECT pg_advisory_xact_lock(301849630151)')
            db.execute('''
                CREATE TABLE IF NOT EXISTS alli_appointment_dispatch (
                    id text PRIMARY KEY,
                    status text NOT NULL CHECK (
                        status IN ('dispatching', 'queued', 'uncertain', 'blocked')
                    ),
                    call_sid text,
                    context jsonb NOT NULL,
                    outcome jsonb,
                    voicemail jsonb,
                    reason text,
                    disposition text CHECK (
                        disposition IN ('queued', 'ringing', 'in-progress',
                                        'completed', 'busy', 'failed', 'no-answer', 'canceled')
                    ),
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    CHECK (status <> 'queued' OR call_sid IS NOT NULL)
                )
            ''')
            db.execute('''
                ALTER TABLE alli_appointment_dispatch
                ADD COLUMN IF NOT EXISTS disposition text
            ''')
            db.execute('''
                ALTER TABLE alli_appointment_dispatch
                ADD COLUMN IF NOT EXISTS voicemail jsonb
            ''')
        # A failed commit must leave initialization retryable.
        _initialized = True


def claim(key: str, context: dict[str, Any]) -> bool:
    """Commit a new dispatch claim before returning permission to dial.

    False means the key already exists, including an uncertain or blocked
    dispatch. Exceptions also mean the caller must not dial.
    """
    initialize()
    with connection() as db:
        row = db.execute('''
            INSERT INTO alli_appointment_dispatch (id, status, context)
            VALUES (%s, 'dispatching', %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING id
        ''', (key, Jsonb(context))).fetchone()
    # The connection context commits before execution can reach this return.
    return row is not None


def get(key: str) -> dict[str, Any] | None:
    """Read a claim without changing its status or making it retryable."""
    initialize()
    with connection() as db:
        row = db.execute('''
            SELECT id, status, call_sid, context, outcome, reason, disposition, voicemail
            FROM alli_appointment_dispatch WHERE id = %s
        ''', (key,)).fetchone()
    if row is None:
        return None
    return dict(zip(
        ('key', 'status', 'call_sid', 'context', 'outcome', 'reason', 'disposition', 'voicemail'), row
    ))


def queued(key: str, call_sid: str) -> bool:
    """Record accepted dispatch, without overwriting any earlier outcome.

    Repeating the same SID is idempotent; a different SID or a terminal status
    returns False and is never overwritten.
    """
    if not isinstance(call_sid, str) or not call_sid.strip():
        raise ValueError('A nonempty call SID is required')
    initialize()
    with connection() as db:
        row = db.execute('''
            UPDATE alli_appointment_dispatch
            SET status = 'queued', call_sid = %s, updated_at = now()
            WHERE id = %s AND (call_sid IS NULL OR call_sid = %s) AND (
                status = 'dispatching' OR (status = 'queued' AND call_sid = %s)
            )
            RETURNING id
        ''', (call_sid, key, call_sid, call_sid)).fetchone()
    return row is not None


def uncertain(key: str) -> bool:
    """Make an ambiguous dispatch terminal; never turn it into a retry."""
    initialize()
    with connection() as db:
        row = db.execute('''
            UPDATE alli_appointment_dispatch
            SET status = 'uncertain', updated_at = now()
            WHERE id = %s AND status IN ('dispatching', 'uncertain')
            RETURNING id
        ''', (key,)).fetchone()
    return row is not None


def blocked(key: str, reason: str) -> bool:
    """Record a pre-dial refusal; existing terminal dispatches stay unchanged."""
    initialize()
    with connection() as db:
        row = db.execute('''
            UPDATE alli_appointment_dispatch
            SET status = 'blocked', reason = %s, updated_at = now()
            WHERE id = %s AND (
                status = 'dispatching' OR (status = 'blocked' AND reason = %s)
            )
            RETURNING id
        ''', (reason, key, reason)).fetchone()
    return row is not None


def record_outcome(key: str, outcome: Any) -> bool:
    """Atomically accept the first outcome, or an identical repeated outcome.

    A callback can arrive while the call is still marked dispatching. Store its
    outcome separately so a later queued() update cannot erase it. Conflicting
    outcomes, missing claims and blocked dispatches return False. An uncertain
    dispatch may still finish and report a valid late outcome.
    """
    if outcome is None:
        raise ValueError('An outcome is required')
    initialize()
    value = Jsonb(outcome)
    with connection() as db:
        row = db.execute('''
            UPDATE alli_appointment_dispatch
            SET outcome = %s,
                updated_at = CASE WHEN outcome IS NULL THEN now() ELSE updated_at END
            WHERE id = %s AND status IN ('dispatching', 'queued', 'uncertain')
                AND (outcome IS NULL OR outcome = %s)
            RETURNING id
        ''', (value, key, value)).fetchone()
    return row is not None


def record_disposition(key: str, call_sid: str, disposition: str) -> bool:
    """Record monotonic Twilio delivery status without changing dispatch status.

    Authenticated callbacks can precede the dispatch response. Bind their SID
    atomically when absent, rejecting any different SID. A terminal disposition
    is final: repeats are accepted, but any conflicting or older callback is
    ignored. An outcome describes the appointment response independently.
    """
    if not isinstance(call_sid, str) or not call_sid.strip():
        raise ValueError('A nonempty call SID is required')
    if disposition not in _DISPOSITION_RANK:
        raise ValueError('Unsupported call disposition')
    initialize()
    with connection() as db:
        row = db.execute('''
            UPDATE alli_appointment_dispatch
            SET call_sid = COALESCE(call_sid, %s), disposition = %s, updated_at = now()
            WHERE id = %s AND status IN ('dispatching', 'queued', 'uncertain')
                AND (call_sid IS NULL OR call_sid = %s)
                AND (
                    disposition IS NULL OR disposition = %s OR (
                        disposition IN ('queued', 'ringing', 'in-progress')
                        AND CASE disposition
                            WHEN 'queued' THEN 0
                            WHEN 'ringing' THEN 1
                            WHEN 'in-progress' THEN 2
                        END < %s
                    )
                )
            RETURNING id
        ''', (call_sid, disposition, key, call_sid, disposition,
              _DISPOSITION_RANK[disposition])).fetchone()
    return row is not None


def claim_voicemail(key: str, call_sid: str, message: str) -> bool:
    """Commit one voicemail attempt before permitting a Twilio redirect.

    The caller prepares the privacy-safe message. Bind an early callback's SID
    only if absent; a different SID or any existing voicemail record prevents
    another attempt, even after a crash or an ambiguous redirect response.
    Exceptions also mean the caller must not redirect.
    """
    if not isinstance(call_sid, str) or not call_sid.strip():
        raise ValueError('A nonempty call SID is required')
    if not isinstance(message, str) or not message.strip():
        raise ValueError('A nonempty voicemail message is required')
    initialize()
    with connection() as db:
        row = db.execute('''
            UPDATE alli_appointment_dispatch
            SET call_sid = COALESCE(call_sid, %s), voicemail = %s, updated_at = now()
            WHERE id = %s AND status IN ('dispatching', 'queued', 'uncertain')
                AND (call_sid IS NULL OR call_sid = %s)
                AND voicemail IS NULL
            RETURNING id
        ''', (call_sid, Jsonb({'status': 'pending', 'message': message}),
              key, call_sid)).fetchone()
    # Commit must complete before the caller can redirect a live call.
    return row is not None


def finish_voicemail(key: str, status: Literal['submitted', 'uncertain']) -> bool:
    """Finalize only a pending attempt without making voicemail retryable.

    Submitted means the redirect was accepted, not proof a voicemail was heard.
    A terminal record cannot be replaced, including by an identical retry.
    """
    if status not in ('submitted', 'uncertain'):
        raise ValueError('Unsupported voicemail status')
    initialize()
    with connection() as db:
        row = db.execute('''
            UPDATE alli_appointment_dispatch
            SET voicemail = jsonb_set(voicemail, '{status}', %s), updated_at = now()
            WHERE id = %s AND voicemail ->> 'status' = 'pending'
            RETURNING id
        ''', (Jsonb(status), key)).fetchone()
    return row is not None
