-- Execution telemetry (spec §66–67, §100). Applied by `aura-workers migrate-clickhouse`.
-- Sharded/replicated variants (ReplicatedMergeTree + Distributed) are used in production;
-- this single-node DDL is for development and CI.

CREATE DATABASE IF NOT EXISTS aura;

CREATE TABLE IF NOT EXISTS aura.executions
(
    event_id          String,
    occurred_at       DateTime64(3, 'UTC'),
    account_id        LowCardinality(String),
    region            LowCardinality(String),
    workspace_id      String,
    environment_id    Nullable(String),
    workload_id       Nullable(String),
    policy_id         Nullable(String),
    trace_id          String,
    execution_id      String,
    session_id        Nullable(String),
    mode              LowCardinality(String),
    status            LowCardinality(String),
    error_code        Nullable(String),
    total_latency_ms  UInt32,
    total_cost_usd    Decimal(18, 8),
    model             LowCardinality(String),
    input_tokens      UInt64,
    output_tokens     UInt64,
    cached_input_tokens UInt64,
    attempts          UInt8,
    router_version    LowCardinality(String),
    routing_reason    String,
    routing_features  String,  -- JSON; offline policy learning (V4) reads this
    propensity        Float32,
    notes             Array(String)
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(occurred_at)
ORDER BY (account_id, occurred_at, event_id)
TTL toDateTime(occurred_at) + INTERVAL 400 DAY;

CREATE TABLE IF NOT EXISTS aura.execution_calls
(
    event_id       String,
    occurred_at    DateTime64(3, 'UTC'),
    account_id     LowCardinality(String),
    execution_id   String,
    seq            UInt8,
    provider       LowCardinality(String),
    model          LowCardinality(String),
    input_tokens   UInt64,
    output_tokens  UInt64,
    cached_input_tokens UInt64,
    latency_ms     UInt32,
    cost_usd       Decimal(18, 8),
    error          Nullable(String)
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(occurred_at)
ORDER BY (account_id, occurred_at, event_id, seq)
TTL toDateTime(occurred_at) + INTERVAL 400 DAY;

-- Daily rollup for dashboards and the X-Ray report.
CREATE TABLE IF NOT EXISTS aura.executions_daily
(
    day            Date,
    account_id     LowCardinality(String),
    model          LowCardinality(String),
    status         LowCardinality(String),
    executions     UInt64,
    input_tokens   UInt64,
    output_tokens  UInt64,
    cost_usd       Decimal(38, 8),
    latency_ms_sum UInt64
)
ENGINE = SummingMergeTree
ORDER BY (account_id, day, model, status);

CREATE MATERIALIZED VIEW IF NOT EXISTS aura.executions_daily_mv TO aura.executions_daily AS
SELECT
    toDate(occurred_at) AS day,
    account_id,
    model,
    status,
    count() AS executions,
    sum(input_tokens) AS input_tokens,
    sum(output_tokens) AS output_tokens,
    sum(total_cost_usd) AS cost_usd,
    sum(total_latency_ms) AS latency_ms_sum
FROM aura.executions
GROUP BY day, account_id, model, status;
