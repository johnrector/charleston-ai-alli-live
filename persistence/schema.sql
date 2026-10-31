-- Alli's operational records. No provider credentials or model-generated SQL.
CREATE TABLE IF NOT EXISTS ea_assistant (
    id text PRIMARY KEY,
    name text NOT NULL,
    principal text NOT NULL,
    timezone text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ea_person (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    name text NOT NULL DEFAULT '',
    email text NOT NULL DEFAULT '',
    kind text NOT NULL DEFAULT 'contact' CHECK(kind IN ('owner','contact')),
    status text NOT NULL DEFAULT 'known' CHECK(status IN ('known','approved','suspended','deleted')),
    attributes jsonb NOT NULL DEFAULT '{}',
    source text NOT NULL,
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(assistant_id,id),
    CHECK(kind <> 'owner' OR status='approved')
);
CREATE UNIQUE INDEX IF NOT EXISTS ea_one_owner ON ea_person(assistant_id) WHERE kind='owner';
CREATE TABLE IF NOT EXISTS ea_endpoint (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assistant_id text NOT NULL,
    person_id uuid NOT NULL,
    kind text NOT NULL CHECK(kind IN ('phone','email')),
    address text NOT NULL,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(assistant_id,person_id) REFERENCES ea_person(assistant_id,id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ea_endpoint_active ON ea_endpoint(assistant_id,kind,address) WHERE active;
CREATE TABLE IF NOT EXISTS ea_conversation (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assistant_id text NOT NULL,
    person_id uuid NOT NULL,
    source_id text NOT NULL,
    channel text NOT NULL CHECK(channel IN ('phone','sms','dot','dashboard')),
    scope text NOT NULL DEFAULT 'friend' CHECK(scope IN ('owner','friend','guest')),
    transcript jsonb NOT NULL DEFAULT '[]',
    finalized boolean NOT NULL DEFAULT false,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    UNIQUE(assistant_id,source_id),
    UNIQUE(assistant_id,id),
    FOREIGN KEY(assistant_id,person_id) REFERENCES ea_person(assistant_id,id)
);
CREATE TABLE IF NOT EXISTS ea_task (
    id text PRIMARY KEY,
    assistant_id text NOT NULL,
    person_id uuid NOT NULL,
    conversation_id uuid,
    parent_id text REFERENCES ea_task(id),
    source_id text NOT NULL,
    title text NOT NULL,
    instruction text NOT NULL,
    scope text NOT NULL CHECK(scope IN ('owner','friend')),
    authority jsonb NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK(state IN
        ('queued','working','waiting','needs_owner','completed','partial','failed','cancelled')),
    result text NOT NULL DEFAULT '',
    context jsonb NOT NULL DEFAULT '{}',
    available_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(assistant_id,source_id),
    UNIQUE(assistant_id,id),
    FOREIGN KEY(assistant_id,person_id) REFERENCES ea_person(assistant_id,id),
    FOREIGN KEY(assistant_id,parent_id) REFERENCES ea_task(assistant_id,id),
    FOREIGN KEY(assistant_id,conversation_id) REFERENCES ea_conversation(assistant_id,id)
);
CREATE TABLE IF NOT EXISTS ea_agent_run (
    task_id text PRIMARY KEY REFERENCES ea_task(id),
    session_id text UNIQUE,
    dispatch_state text NOT NULL DEFAULT 'ready' CHECK(dispatch_state IN ('ready','creating','uncertain','bound')),
    environment_kind text NOT NULL DEFAULT 'none' CHECK(environment_kind IN ('none','openai_hosted','self_hosted')),
    environment_id text,
    remote_url_sealed text,
    turn_id text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ea_agent_input (
    id text PRIMARY KEY,
    task_id text NOT NULL REFERENCES ea_task(id),
    body text NOT NULL,
    sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ea_action (
    task_id text NOT NULL REFERENCES ea_task(id),
    call_id text NOT NULL,
    tool text NOT NULL,
    arguments jsonb NOT NULL,
    intent text NOT NULL,
    writes boolean NOT NULL,
    state text NOT NULL DEFAULT 'attempting' CHECK(state IN ('attempting','done','uncertain')),
    receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(task_id,call_id),
    UNIQUE(task_id,intent)
);
CREATE TABLE IF NOT EXISTS ea_commitment (
    id text PRIMARY KEY,
    assistant_id text NOT NULL,
    person_id uuid NOT NULL,
    task_id text,
    kind text NOT NULL CHECK(kind IN ('sms','call','work','reminder')),
    instruction text NOT NULL,
    due_at timestamptz NOT NULL,
    expires_at timestamptz,
    state text NOT NULL DEFAULT 'planned' CHECK(state IN ('planned','queued','completed','blocked','cancelled')),
    context jsonb NOT NULL DEFAULT '{}',
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(assistant_id,id),
    FOREIGN KEY(assistant_id,person_id) REFERENCES ea_person(assistant_id,id),
    FOREIGN KEY(assistant_id,task_id) REFERENCES ea_task(assistant_id,id),
    CHECK(expires_at IS NULL OR expires_at>due_at)
);
CREATE INDEX IF NOT EXISTS ea_commitment_due ON ea_commitment(due_at) WHERE state='planned';
-- Native OpenAI schedules define time; these rows retain authority and receipts.
CREATE TABLE IF NOT EXISTS ea_schedule (
    id text PRIMARY KEY,
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    person_id uuid NOT NULL,
    source_task_id text NOT NULL REFERENCES ea_task(id),
    source_id text NOT NULL,
    title text NOT NULL,
    instruction text NOT NULL,
    timing jsonb NOT NULL,
    authority jsonb NOT NULL,
    state text NOT NULL DEFAULT 'creating',
    provider_id text UNIQUE,
    launch_digest text UNIQUE NOT NULL,
    launch_sealed text NOT NULL,
    control_state text NOT NULL DEFAULT '',
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(assistant_id,source_id),
    FOREIGN KEY(assistant_id,person_id) REFERENCES ea_person(assistant_id,id)
);
CREATE TABLE IF NOT EXISTS ea_schedule_run (
    invocation_id text PRIMARY KEY,
    schedule_id text NOT NULL REFERENCES ea_schedule(id),
    native_session_id text NOT NULL,
    task_id text NOT NULL REFERENCES ea_task(id),
    scheduled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ea_text_turn (
    task_id text PRIMARY KEY REFERENCES ea_task(id),
    state text NOT NULL DEFAULT 'generating',
    response_id text,
    response jsonb,
    work_task_id text REFERENCES ea_task(id),
    lease_until timestamptz NOT NULL DEFAULT now()+interval '90 seconds',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ea_delivery (
    id text PRIMARY KEY,
    assistant_id text NOT NULL,
    source_id text NOT NULL,
    person_id uuid NOT NULL,
    task_id text,
    commitment_id text,
    channel text NOT NULL CHECK(channel IN ('sms','email','phone','dot')),
    destination text NOT NULL,
    body text NOT NULL,
    due_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    state text NOT NULL DEFAULT 'queued' CHECK(state IN
        ('queued','attempting','accepted','delivered','failed','uncertain','cancelled')),
    provider_id text UNIQUE,
    forwarded_state text,
    receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(assistant_id,source_id),
    FOREIGN KEY(assistant_id,person_id) REFERENCES ea_person(assistant_id,id),
    FOREIGN KEY(assistant_id,commitment_id) REFERENCES ea_commitment(assistant_id,id),
    FOREIGN KEY(assistant_id,task_id) REFERENCES ea_task(assistant_id,id)
);
CREATE INDEX IF NOT EXISTS ea_delivery_due ON ea_delivery(due_at) WHERE state='queued';
CREATE TABLE IF NOT EXISTS ea_phone_call (
    id text PRIMARY KEY,
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    conversation_id uuid NOT NULL,
    person_id uuid NOT NULL,
    parent_task_id text,
    source_id text NOT NULL,
    direction text NOT NULL CHECK(direction IN ('inbound','outbound')),
    number_verified boolean NOT NULL DEFAULT false,
    max_seconds integer NOT NULL DEFAULT 1800,
    instruction text NOT NULL DEFAULT '',
    allowed_tools jsonb,
    invitation_email text NOT NULL DEFAULT '',
    expected_from text NOT NULL,
    expected_to text NOT NULL,
    state text NOT NULL CHECK(state IN ('queued','attempting','accepted','finished','uncertain','cancelled')),
    provider_id text UNIQUE,
    carrier_status text NOT NULL DEFAULT '',
    duration_seconds integer,
    media_connected_at timestamptz,
    native_finalized boolean NOT NULL DEFAULT false,
    voice_usage jsonb,
    ended_at timestamptz,
    reported_at timestamptz,
    reported_finalized boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(assistant_id,source_id),
    UNIQUE(assistant_id,conversation_id),
    FOREIGN KEY(assistant_id,conversation_id) REFERENCES ea_conversation(assistant_id,id),
    FOREIGN KEY(assistant_id,person_id) REFERENCES ea_person(assistant_id,id),
    FOREIGN KEY(assistant_id,parent_task_id) REFERENCES ea_task(assistant_id,id)
);
CREATE INDEX IF NOT EXISTS ea_phone_queue ON ea_phone_call(created_at) WHERE state='queued';
CREATE UNIQUE INDEX IF NOT EXISTS ea_phone_one_attempt ON ea_phone_call(parent_task_id,person_id)
    WHERE direction='outbound';
CREATE TABLE IF NOT EXISTS ea_collection (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    name text NOT NULL,
    definition jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(assistant_id,name),
    UNIQUE(assistant_id,id)
);
CREATE TABLE IF NOT EXISTS ea_record (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assistant_id text NOT NULL,
    collection_id uuid NOT NULL,
    body jsonb NOT NULL,
    revision bigint NOT NULL DEFAULT 1,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(assistant_id,collection_id) REFERENCES ea_collection(assistant_id,id)
);
CREATE INDEX IF NOT EXISTS ea_record_collection ON ea_record(assistant_id,collection_id,updated_at DESC) WHERE deleted_at IS NULL;
CREATE TABLE IF NOT EXISTS ea_ledger (
    id bigserial PRIMARY KEY,
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    person_id uuid,
    task_id text,
    source_id text NOT NULL,
    kind text NOT NULL,
    facts jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(assistant_id,source_id),
    FOREIGN KEY(assistant_id,person_id) REFERENCES ea_person(assistant_id,id),
    FOREIGN KEY(assistant_id,task_id) REFERENCES ea_task(assistant_id,id)
);
CREATE INDEX IF NOT EXISTS ea_ledger_person ON ea_ledger(assistant_id,person_id,id DESC);
-- Activity is an audit of operations, not a recursive copy of every lookup.
-- Project old and new read receipts alike; immutable raw evidence stays saved.
CREATE OR REPLACE VIEW ea_activity AS
SELECT l.id,l.assistant_id,l.person_id,l.task_id,l.source_id,l.kind,
    CASE WHEN l.kind='action.receipt' AND a.writes=false THEN
        jsonb_build_object('call_id',a.call_id,'tool',a.tool,'read',true,
            'receipt',jsonb_build_object('ok',a.receipt->'ok',
                'error',a.receipt->'error','lookup_data_omitted',true))
    ELSE l.facts END AS facts,l.created_at
FROM ea_ledger l LEFT JOIN ea_action a ON l.kind='action.receipt'
    AND a.task_id=l.task_id AND a.call_id=l.facts->>'call_id';
CREATE TABLE IF NOT EXISTS ea_tool_grant (
    digest text PRIMARY KEY,
    assistant_id text NOT NULL,
    task_id text NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(assistant_id,task_id) REFERENCES ea_task(assistant_id,id)
);
CREATE TABLE IF NOT EXISTS ea_auth_challenge (
    digest text PRIMARY KEY,
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    code_digest text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    consumed boolean NOT NULL DEFAULT false,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ea_auth_recent ON ea_auth_challenge(assistant_id,created_at);
CREATE TABLE IF NOT EXISTS ea_booking (
    action_key text PRIMARY KEY,
    assistant_id text NOT NULL,
    person_id uuid NOT NULL,
    task_id text NOT NULL,
    event_id text NOT NULL,
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(assistant_id,person_id) REFERENCES ea_person(assistant_id,id),
    FOREIGN KEY(assistant_id,task_id) REFERENCES ea_task(assistant_id,id)
);
CREATE TABLE IF NOT EXISTS ea_owner_session (
    digest text PRIMARY KEY,
    assistant_id text NOT NULL,
    person_id uuid NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY(assistant_id,person_id) REFERENCES ea_person(assistant_id,id)
);
CREATE TABLE IF NOT EXISTS ea_oauth_record (
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    kind text NOT NULL,
    digest text NOT NULL,
    sealed text NOT NULL,
    expires_at double precision NOT NULL,
    PRIMARY KEY(assistant_id,kind,digest)
);

CREATE TABLE IF NOT EXISTS ea_subscription (
    id text PRIMARY KEY,
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    principal text NOT NULL,
    sealed text NOT NULL,
    filters jsonb NOT NULL,
    expires_at timestamptz NOT NULL,
    cursor bigint NOT NULL DEFAULT 0,
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS ea_event_delivery (
    subscription_id text NOT NULL REFERENCES ea_subscription(id),
    ledger_id bigint NOT NULL REFERENCES ea_ledger(id),
    event_id text NOT NULL,
    payload jsonb NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    due_at timestamptz NOT NULL DEFAULT now(),
    state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','attempting','delivered','failed')),
    PRIMARY KEY(subscription_id,ledger_id)
);
CREATE TABLE IF NOT EXISTS ea_device (
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    id text NOT NULL,
    last_seen timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(assistant_id,id)
);
CREATE TABLE IF NOT EXISTS ea_device_nonce (
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    nonce text NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY(assistant_id,nonce)
);
CREATE TABLE IF NOT EXISTS ea_account_connection (
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    provider text NOT NULL CHECK(provider IN ('google','shopify')),
    details jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(assistant_id,provider)
);
CREATE TABLE IF NOT EXISTS ea_account_oauth_flow (
    digest text PRIMARY KEY,
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    provider text NOT NULL CHECK(provider IN ('google','shopify')),
    owner_session_digest text NOT NULL,
    verifier_sealed text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ea_account_secret (
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    provider text NOT NULL CHECK(provider IN ('shopify')),
    sealed text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(assistant_id,provider)
);

-- One owner-invoked diagnostic per local day, including uncertain attempts.
CREATE TABLE IF NOT EXISTS ea_diagnostic_run (
    assistant_id text NOT NULL REFERENCES ea_assistant(id),
    check_date date NOT NULL,
    request_id text NOT NULL,
    release text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    result jsonb,
    PRIMARY KEY(assistant_id,check_date),
    UNIQUE(assistant_id,request_id)
);
