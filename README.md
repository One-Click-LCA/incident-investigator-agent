# Incident Investigator Agent

An AI agent that investigates ECS service incidents and produces a structured Root Cause Analysis (RCA). Given a service name, environment, and optional incident description, it autonomously queries AWS, logs, databases, and GitHub — then writes a Markdown + JSON report.

---

## How It Works

The agent runs in two sequential phases.

### Phase 1 — Bootstrap (deterministic)

Before any AI reasoning begins, `bootstrap.py` runs a fixed set of lookups:

1. **Load credentials** — pulls the `ocl-devops-ai-automation` secret from AWS Secrets Manager into a module-level singleton (`config.py`). No credentials are logged or printed.
2. **Discover the ECS service** — scans all ECS clusters for a service matching the given name. If multiple matches exist (e.g. `supply-chain-service-comp-tools`), it scores them by env match and picks the best fit.
3. **Detect dependencies** — reads the task definition's environment variables and Secrets Manager references to detect Mongo, Redis, RDS, and ELK without making any database calls.
4. **Detect framework** — infers Spring Boot, Grails, Node.js, or Python from the container image name and env var patterns. For JVM services it adjusts memory warning thresholds (JVM pre-allocates heap, so 85–93% is normal).

The bootstrap result — service context, dependencies, framework hints — is injected as a structured message into the orchestrator's initial prompt. It is not passed through LangGraph state.

### Phase 2 — Orchestrator (LLM-driven)

`agent.py` builds a DeepAgent orchestrator using Claude Sonnet on AWS Bedrock. The orchestrator's tools are the specialist agents — and only the specialists relevant to the detected dependencies are registered. If MongoDB was not detected, `invoke_mongo_analyst` never appears as a tool.

**Always registered:** `invoke_ecs_analyst`, `invoke_log_analyst`, `invoke_topology_analyst`, `write_rca`

**Conditionally registered:**

| Condition | Specialists added |
|---|---|
| GitHub token present | `invoke_code_analyst`, `invoke_framework_analyst`, `invoke_data_domain_analyst` |
| Mongo detected | `invoke_mongo_analyst` |
| Redis detected + env != prod | `invoke_redis_analyst` |
| RDS detected | `invoke_rds_analyst` |

The orchestrator is given a thin system prompt: what each specialist does, the principle that evidence should drive which specialist to call next, and a hard rule to call **one specialist at a time**. There is no hardcoded decision tree — the LLM reasons from findings to next steps.

The orchestrator starts with ECS and logs (baseline), then follows the evidence. A clean bill of health (stable ECS, silent logs, no risky deploy) is a valid outcome — the agent will not keep calling specialists when there is nothing to find.

---

## Specialist Agents

Each specialist is itself a DeepAgent (LLM + tools + system prompt). The orchestrator calls one at a time, reads its full result, then decides what to call next. Specialists stream their intermediate tool calls to stdout.

| Specialist | Responsibility | Tools |
|---|---|---|
| `ecs_analyst` | ECS service health, stopped task exit codes, CPU/memory trends, ALB target health, autoscaling events, recent deployments | `ecs.py`, `cloudwatch.py` |
| `log_analyst` | ELK error counts, exception stack traces, rate of change vs baseline | `elk.py` |
| `code_analyst` | Recent GitHub commits, Jenkins deployment history, risky file changes (domain classes, config) | `code_change.py`, `jenkins.py` |
| `framework_analyst` | Reads `application.yml`, `pom.xml` from GitHub at the deployed commit; matches log exceptions to known root causes (MongoTimeoutException → pool exhausted, MissingPropertyException → schema drift, etc.); flags config risks like low thread pools or retry-without-backoff | `code_change.py` |
| `mongo_analyst` | Atlas cluster state and metrics, slow query suggestions (COLLSCAN = no index), open alerts, direct server stats via PyMongo | `mongo.py` |
| `redis_analyst` | Redis memory, eviction counts, slowlog, cluster info (preprod only) | `redis.py` |
| `rds_analyst` | RDS CloudWatch metrics (CPU, connections, latency), active queries, lock waits, table bloat (dead tuple ratio) | `rds.py`, `cloudwatch.py` |
| `topology_analyst` | Checks if upstream ECS services and external dependencies (Keycloak, Typesense, etc.) are reachable via DNS + TCP probe | `ecs.py`, `elk.py`, `external.py` |
| `data_domain_analyst` | Reads domain model files from GitHub to detect schema drift when `MissingPropertyException` is found in logs | `code_change.py` |

---

## Credential Flow

All credentials come from a single AWS Secrets Manager secret (`ocl-devops-ai-automation`). Bootstrap loads them once into `config.py`; tools read from that singleton. No credential is logged or written to disk.

```
AWS Secrets Manager (ocl-devops-ai-automation)
    │
    └── bootstrap.py → config.py singleton
            │
            ├── ELK_NONPROD_URL / ELK_PROD_URL ──────────────→ elk.py
            ├── REDIS_NONPROD_URL ────────────────────────────→ redis.py
            ├── MDB_NONPROD_CLIENT_ID/SECRET/PROJECT_ID ──────→ mongo.py (Atlas OAuth)
            ├── JENKINS_NONPROD_URL / TOKEN / jenkins_user ───→ jenkins.py
            ├── GITHUB_TOKEN ────────────────────────────────→ code_change.py
            └── AWS session (profile / EC2 instance role) ───→ ecs.py, rds.py, cloudwatch.py
```

RDS credentials are looked up per-service from a separate Secrets Manager secret. The naming convention is `rds-{service-base-name}-dev` (preprod) or `rds-{service-base-name}-prod`. The agent tries several candidate names — stripping env suffixes and `-service` tail — before falling back to an RDS API describe call.

---

## Output

`write_rca` is the terminal tool. When the orchestrator calls it, the investigation ends and two files are written to `output/{service}-{env}-{timestamp}/`:

- `rca.json` — structured evidence bundle
- `rca.md` — human-readable Markdown report rendered from a Jinja2 template

The RCA includes: incident summary, symptoms, root cause, confidence level (high / medium / low), causal chain, per-analyst evidence with severity, contributing factors, recommended actions, and any tool failures or gaps.

---

## Project Layout

```
main.py                        CLI entry point
src/
  bootstrap.py                 Phase 1: credentials, ECS discovery, dep/framework detection
  config.py                    Singleton: credentials + runtime context
  agent.py                     Phase 2: build orchestrator, register tools, stream output
  system_prompt.py             Thin orchestrator prompt
  utils.py                     safe_call, tool_result, truncate helpers
  agents/
    _base.py                   Shared model builder + specialist streaming helper
    ecs_analyst.py
    log_analyst.py
    code_analyst.py
    framework_analyst.py
    mongo_analyst.py
    redis_analyst.py
    rds_analyst.py
    topology_analyst.py
    data_domain_analyst.py
  tools/
    ecs.py                     AWS ECS + ALB + autoscaling
    cloudwatch.py              CloudWatch metrics + alarms
    elk.py                     ELK log search
    code_change.py             GitHub REST API
    jenkins.py                 Jenkins REST API
    mongo.py                   Atlas OAuth API + PyMongo
    redis.py                   redis-py (read-only)
    rds.py                     CloudWatch + psycopg2 (read-only)
    external.py                DNS + TCP reachability probe
    rca.py                     write_rca terminal tool
  output/
    writer.py                  Saves rca.json + renders rca.md
    rca_template.md.j2         Jinja2 Markdown template
```

---

## Running on EC2

```bash

cd /opt/incident-investigator-agent
source /opt/.venv/bin/activate
pip install -e . -q

# General health check
python main.py --service supply-chain-service --env preprod --minutes 30

# With incident description and focused window
python main.py --service supply-chain-service --env preprod \
  --reason "502s spiking on /api/orders after 14:30 deploy" \
  --minutes 60

# Focus on a specific area (still runs ECS + logs first)
python main.py --service billing-service --env preprod \
  --reason "slow queries reported" --focus mongo --minutes 45
```

RCA output is written to `./output/{service}-{env}-{timestamp}/rca.md`.
