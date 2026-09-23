# OCI Sandbox Factory: architecture

A user says what they need, in a form or in a chat with an AI, and gets a
temporary environment on Oracle Cloud with an Autonomous Database, Kafka and
their own container, on an Oracle HTTPS hostname, in about 5 minutes. Every
sandbox is created and removed by one Terraform stack in OCI Resource Manager
and deletes itself after at most 3 days.

## Flow

```
user (browser)
  │  chat / form                                   ┌─────────────────────────┐
  ▼                                                │ OCI Generative AI       │
APEX page ──Ajax──▶ PL/SQL callback ──plan/chat──▶ │ Gemini 2.5 Flash        │
  ▲                    │  insert QUEUED             └─────────────────────────┘
  │ poll 5 s           ▼
  └──────────── SBX.SANDBOX_REQUESTS ◀──── poll / claim / log / outputs ──── worker.py
                                                                              │ buildx arm64 + push ──▶ OCI Container Registry
                                                                              │ zip stack + APPLY   ──▶ OCI Resource Manager (Terraform)
                                                                              │                            └─▶ sbx-sandboxes: ATP, Streaming, Container, NSG, API Gateway
                                                                              └ every 30 min: reap expired, reconcile
```

| Step | Who | What | Time |
|---|---|---|---|
| 1 | APEX page | Chat or form; the AI answers with JSON: reply + optional action | 3-8 s |
| 2 | PL/SQL callback | Inserts one row: sandbox id, lifetime 1-3 days, switches, image / Git URL / folder, port | instant |
| 3 | Worker | Claims the row (`FOR UPDATE SKIP LOCKED`), sets RUNNING | ≤ 10 s |
| 4 | Worker | Folder or Git: `docker buildx --platform linux/arm64 --push` to a public OCIR repo | 1-4 min |
| 5 | Worker | Zips `stacks/sandbox`, creates/updates the Resource Manager stack, runs APPLY | 3-5 min |
| 6 | Resource Manager | Terraform creates ATP, Streaming pool + topic, Container Instance, NSG, API Gateway, tags | in step 5 |
| 7 | Worker | Streams the log into the row, writes outputs (URLs, connect string, ADMIN password), DONE | instant |
| 8 | Reaper | Every 30 min destroys stacks past `expires`; reconciles table vs stacks | background |

The AI only proposes. Nothing is created until the user clicks the action
button, which inserts the row. The worker is the only component holding cloud
credentials.

## Foundation (once per tenancy, `foundation/`)

| Piece | What | Why |
|---|---|---|
| Compartments | `sbx` → `sbx-control` (shared infra), `sbx-sandboxes` (all sandboxes) | Budget and quotas attach to `sbx`; `per_sandbox_compartment = true` gives each sandbox its own |
| Tags `sbx.*` | `owner`, `sandbox_id`, `team`, `expires` (cost-tracking) | Reaper reads `expires`; `owner` stamped by a tag default |
| Budget | Monthly, alerts at 50/80/100 % + forecast | Backstop |
| Quotas | ADB ECPUs/free count, A1 cores/memory, OKE clusters, partitions; GPU + bare metal zero | No accidental spend |
| Network | One VCN: public subnet (IGW), private subnet (NAT + service gateway) | Sandboxes attach, never build their own |
| Control DB | Always Free ATP `SBXCTL` | APEX app, queue, AI calls |
| Dynamic group + policy | Control DB may use Generative AI | Database calls the model as itself |

## Sandbox stack (`stacks/sandbox/`)

| Switch | Module | Creates |
|---|---|---|
| `enable_adb` | `modules/adb` | ATP; free tier public + IP allow-list + mTLS off, or paid ECPU with private endpoint + NSG; random ADMIN password; connect string, SQL Developer Web and APEX URLs as outputs |
| `enable_kafka` | `modules/kafka` | OCI Streaming pool (Kafka API) + one stream per topic; or a managed Kafka DEVELOPMENT cluster (untested, hourly billed) |
| `enable_app` | `modules/app` | Container Instance (A1 ARM, 1 OCPU / 4 GB), NSG with only the declared ports, API Gateway with an Oracle HTTPS hostname to the first port |

Containers receive `SANDBOX_ID`, `SANDBOX_EXPIRES`, `ADB_DB_NAME`,
`ADB_CONNECT_STRING`, `ADB_ADMIN_PASSWORD`, `KAFKA_BOOTSTRAP_SERVERS`.
`ttl_days` is validated 1-3; `expires` is frozen at creation and tagged on every
resource; the sandbox id must match `^[a-z][a-z0-9-]{1,19}$`.

## Front end and AI

APEX app 112, workspace `SBX`, on the control DB. The home page is one Ajax
region (`factory/apex_home/home.html`) with one callback
(`factory/apex_home/ajax.plsql`): actions `plan`, `chat`, `submit`, `status`.

1. The page sends the last 12 chat turns.
2. The callback builds a SYSTEM message: building blocks, the 3-day rule, the user's sandboxes as JSON.
3. `DBMS_CLOUD.SEND_REQUEST` with credential `OCI$RESOURCE_PRINCIPAL` calls the Generative AI chat endpoint (`google.gemini-2.5-flash`, Phoenix). No API key exists anywhere.
4. The model answers with one JSON object: `reply` + optional `action` (create / deploy / destroy). The page renders the reply and a confirm button.
5. The click calls `submit` → one queue row. `status` returns the caller's latest row per sandbox.

The app is built by code: `apex_builder.py` drives the Create App wizard with
Playwright (APEX has no create-app API); `apex_customize.py` exports with
`APEX_EXPORT`, rewrites the form items, injects the home page, installs with
`APEX_APPLICATION_INSTALL`. Users are APEX accounts (`controldb.py users`).

## Worker, reaper, guardrails

`factory/worker.py` runs where Docker and the OCI config are (Windows task
`SandboxFactory\Worker`, retriggered every 10 min). It calls
`factory/sandbox_factory.py` (`create`, `deploy`, `list`, `destroy`, `reap`),
also exposed as MCP tools in `factory/mcp_server.py`.

Auto-destroy: reaper every 30 min in the worker; daily task
`SandboxFactory\Reap`; the 3-day cap in a check constraint, Terraform
validation, CLI choices and the form. Security: credentials only on the worker
machine; database uses its resource principal; each app opens only its port;
ADMIN password is a sensitive output read from state and shown only to the
requester; budget + quotas cap spend.

## Repository

| Path | Contents |
|---|---|
| `foundation/` | Compartments, tags, budget, quotas, VCN, control ATP, GenAI policy |
| `stacks/sandbox/` | The stack, `schema.yaml`, modules `adb`, `kafka`, `app` |
| `factory/sandbox_factory.py` | CLI |
| `factory/worker.py` | Queue worker + reaper + reconcile |
| `factory/controldb.py` | Control DB setup, APEX workspace, users |
| `factory/apex_builder.py`, `apex_customize.py`, `apex_home/` | APEX app build and home page |
| `factory/mcp_server.py` | MCP tools |
| `examples/` | orders-app, schemagate-mcp, schemagate-studio |
| `docs/DEMO-RUNBOOK.md` | Demo steps |

## Limits and next steps

| Gap | Fix |
|---|---|
| Always Free: 2 ADBs per tenancy, control DB uses one → one free-ADB sandbox at a time | Paid ECPU per sandbox, or one shared paid ATP with a schema per sandbox |
| Worker on a laptop | Container Instance in `sbx-control` with resource principal; kaniko builds in OCI |
| Local folder deploys only work on the worker machine | Git URL / public image for others |
| Managed Kafka cluster mode untested | One test run (hourly billed) |
| One container per request in the UI | Containers list in the form; planner proposes producer + UI pairs |
| APEX accounts, no SSO | Identity domain authentication |
| API Gateway hostname needs ~2 min DNS after creation | Poll before DONE |

Order: OCI-hosted worker, SSO, multi-container requests, OKE tier.
