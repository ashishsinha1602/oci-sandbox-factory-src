# OCI Sandbox Factory

Self-service, auto-expiring sandboxes on Oracle Cloud. A user picks what they
need (Autonomous Database, Kafka, a containerised app), gets those pieces in
the shared `sbx-sandboxes` compartment (or a compartment of their own with
`per_sandbox_compartment = true`), and the sandbox destroys itself after at
most 3 days.

Everything is Terraform run by OCI Resource Manager. Nothing is hardcoded:
moving from a personal tenancy to a company one is a new `.tfvars` file.

```
user (form / CLI / chat agent)
        │
        ▼
factory/sandbox_factory.py ──► OCI Resource Manager (managed Terraform)
                                       │ runs stacks/sandbox
                                       ▼
   sbx                          ← budget + quotas
   ├── sbx-control              ← shared VCN, stacks, container images
   └── sbx-sandboxes
       └── (shared) ADB, Kafka, containers per sandbox, told apart by name + sbx.* tags
           ├── ADB (free or ECPU)
           ├── Kafka (Streaming or managed cluster)
           └── Container Instance (1..n containers)
                                       │
                                       ▼
                           reap: destroy past sbx.expires
```

## Layout

| Path | What |
|---|---|
| `foundation/` | Run once per tenancy: compartments, tags, budget, quotas, shared VCN |
| `stacks/sandbox/` | One sandbox. Resource Manager stack with `schema.yaml` form |
| `stacks/sandbox/modules/{adb,kafka,app}` | The optional pieces |
| `factory/sandbox_factory.py` | CLI: create, deploy, list, destroy, reap |
| `factory/mcp_server.py` | Same functions as MCP tools for a chat agent |
| `foundation/control_adb.tf` | Always Free ATP in sbx-control: APEX front end + request queue |
| `factory/controldb.py` | Control DB setup: SBX schema, `sandbox_requests`, APEX workspace, end users |
| `factory/apex_builder.py` | Builds the APEX app through the App Builder wizard (Playwright) |
| `factory/apex_customize.py` | Export → rewrite form items → re-import (switches, select lists, defaults) |
| `factory/worker.py` | Turns queued rows into sandboxes; streams the log back into the row |
| `examples/hello-app/` | Sample Dockerfile for `deploy` |

## Install it

The foundation is a Resource Manager stack, so a tenancy admin can install it
without cloning anything or running Terraform locally:

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/OWNER/oci-sandbox-factory/releases/latest/download/sandbox-factory-foundation.zip)

It asks for a parent compartment, a budget and an alert email, then creates the
compartment tree, the `sbx` tag namespace, the budget and alerts, the optional
quotas and the VCN. Leave **Create hard quotas** off on the first install: quota
names differ per region, and you can turn them on once you have confirmed them.

Then point the factory at it and install the self-service page:

```bash
export SBX_ADMIN_PASSWORD=...              # control database ADMIN
export SBX_REGISTRY_PREFIX=phx.ocir.io/<your-namespace>/sbx/
export SBX_GENAI_REGION=us-phoenix-1       # a region that serves OCI Generative AI
cd factory
python controldb.py setup                  # schema, tables, APEX workspace
python apex_customize.py                   # install the home page
python worker.py                           # the request worker
```

`SBX_REGISTRY_PREFIX` and `SBX_GENAI_REGION` are stored in `SBX.FACTORY_CONFIG`
and read by the page at runtime, so nothing in the application carries a
tenancy, a region or a registry of its own.

To build the zips yourself: `python release.py` writes both into `dist/`.
Tagging `v*` does it in CI and attaches them to the GitHub release.

Everything below is the manual path, and what each piece actually does.

## 1. Foundation (once per tenancy)

```bash
cd foundation
cp personal.tfvars.example personal.tfvars   # fill in OCIDs, email, budget
terraform init
terraform plan  -var-file=personal.tfvars -out=f.tfplan
terraform apply f.tfplan
```

Creates 28 resources, all free: three compartments, a `sbx` tag namespace
(`owner`, `sandbox_id`, `team`, `expires`), a monthly budget with alerts at
50/80/100% plus a forecast alert, hard quotas (A1 cores, ADB ECPUs, OKE
clusters, stream partitions, GPUs and bare metal zeroed), and one VCN with a
public subnet (internet gateway) and a private subnet (NAT + service gateway).

Quota names differ per service family. The ones here were validated on
`us-phoenix-1`; check `oci limits definition list --service-name compute`
before enabling on another tenancy.

## 2. Sandboxes

```bash
export SBX_OWNER=you@example.com
python factory/sandbox_factory.py create demo1 --adb --kafka --app --ttl 3
python factory/sandbox_factory.py list
python factory/sandbox_factory.py destroy demo1
```

`create` zips `stacks/sandbox`, uploads it as a Resource Manager stack in
`sbx-control`, runs an apply job, and prints the outputs: ADB connect string
and SQL Developer Web URL, Kafka bootstrap servers, the app's public URL.
Re-running `create` with different flags updates the same stack.

The same stack works from the console: Resource Manager → Create stack →
upload the folder as a zip. `schema.yaml` renders the form.

Or locally, for development:

```bash
cd stacks/sandbox
terraform init
terraform plan -var-file=demo1.tfvars    # see demo1.tfvars pattern in the repo history
```

### What each switch creates

| Switch | Default | Notes |
|---|---|---|
| `--adb` | Always Free ATP, public endpoint, IP allow-list, mTLS off | `--adb-tier paid` = ECPU with a private endpoint in the private subnet |
| `--kafka` | OCI Streaming pool with the Kafka-compatible endpoint | `--kafka-mode cluster` = managed Kafka DEVELOPMENT cluster (billed hourly) |
| `--app` | One `nginx:alpine` container, public IP, port 80 opened to `--allowed-cidr` | Ports are opened per container; all containers share one network namespace |

Every container gets `SANDBOX_ID`, `SANDBOX_EXPIRES`, and when present
`ADB_DB_NAME`, `ADB_CONNECT_STRING`, `ADB_ADMIN_PASSWORD`,
`KAFKA_BOOTSTRAP_SERVERS` as environment variables.

## 3. Deploy a local app from a prompt

```bash
python factory/sandbox_factory.py deploy myapp ./examples/hello-app --port 8080 --adb
```

Builds the Dockerfile for `linux/arm64` (Container Instances run on Ampere A1
by default; pass `--shape CI.Standard.E4.Flex` for x86), pushes it to a public
repo in your tenancy's container registry, then creates or updates the
sandbox so the container runs there.

Needs one auth token for the registry login: Console → Profile → Auth tokens
→ Generate. Save it to `~/.oci/ocir_token` or export `OCIR_TOKEN`.

## 4. Auto-destroy

```bash
python factory/sandbox_factory.py reap --dry-run
python factory/sandbox_factory.py reap
```

Reads the `expires` tag on every factory stack and runs a destroy job for
each one past its date. Schedule it daily (cron, Task Scheduler, or an OCI
Function on a Resource Scheduler). The budget alert is the backstop.

## 5. APEX front end (self-service form)

```
APEX app "Sandbox Factory"  ──insert──▶  SBX.SANDBOX_REQUESTS  ◀──poll──  worker.py ──▶ factory ──▶ Resource Manager
        (any browser)                     status / outputs / log                (where Docker + OCI creds live)
```

One-time setup, after the foundation stack is applied (it now includes the control ATP):

```bash
cd factory
SBX_SECRETS_FILE=/tmp/sbx.json python controldb.py setup        # schema, table, view, workspace
SBX_SECRETS_FILE=/tmp/sbx.json python apex_builder.py           # creates the app via the wizard
python apex_customize.py                                        # polishes the request form
python controldb.py users alice@example.com bob@example.com     # end-user accounts
python worker.py                                                # leave running
```

URL: `<control ADB ORDS URL>/r/sbx/sandbox-factory`. Users sign in with an
APEX account. The home page (all Ajax, no page reloads) offers four paths:

| Path | What happens |
|---|---|
| **Chat with the factory** | Conversation with OCI Generative AI (Gemini 2.5 Flash). It knows the user's sandboxes, answers status questions, and proposes create / deploy / destroy actions that the user confirms with one click. |
| **Describe it, AI plans it** | Plain-English request → the AI returns the pieces, a summary, next steps and tips → "Looks good, create it". |
| **Build an app** | Switches for ADB, Kafka, app; image or Git URL; port; lifetime 1-3 days. |
| **Just deploy my container** | Image or Git URL + port, nothing else. |

Below the form, a live status panel polls every 5 s while a request is
QUEUED or RUNNING and shows the log tail, then the URLs and connect strings.
**Requests** and **Sandboxes** are the classic report pages.

The AI runs inside the database: `DBMS_CLOUD.SEND_REQUEST` calls the
Generative AI chat endpoint with the database's resource principal
(`foundation/control_adb.tf` creates the dynamic group and policy), so there
are no API keys anywhere. Lifetime is capped at 3 days by a check constraint.

Home page source: `factory/apex_home/home.html` (region) and
`factory/apex_home/ajax.plsql` (callback: plan, chat, submit, status);
`apex_customize.py` injects both into the export on every run.

The wizard-built app is reproducible: `apex_blueprint.json` is the wizard
blueprint, `apex_customize.py` is idempotent. APEX has no create-app API, so
`apex_builder.py` drives the UI with Playwright (verified on APEX 26.1).

## 6. Demo script: app + database, use it, destroy it

`examples/orders-app` is a real app: it reads the injected `ADB_CONNECT_STRING`
and `ADB_ADMIN_PASSWORD`, creates an `ORDERS` table on first start, and serves
a page where you add orders that land in the sandbox's ATP.

1. In the app's **Chat**: *"deploy my orders app with a database for 2 days"* →
   the AI proposes a create with ADB + the image → **Create it**.
   (Or from a terminal: `python factory/sandbox_factory.py deploy orders-demo ./examples/orders-app --port 8080 --adb --ttl 2`.)
2. Watch the status card: QUEUED → RUNNING (Terraform log streams) → DONE with
   the app URL and the SQL Developer Web link.
3. Open the URL, add a few orders, then open SQL Developer Web and
   `select * from orders` — same rows, in the Autonomous Database.
4. In Chat: *"destroy orders-demo"* → **Destroy** → the compartment, database
   and container are gone in about 3 minutes. The reaper would have done the
   same after 2 days.

Everything in that lifecycle is one Terraform stack in Resource Manager:
compartment, ADB, container, network rules, tags.

## 7. How a user deploys their own app

| They have | They do |
|---|---|
| A public image | Chat: *"deploy nginx:alpine on port 80"* or the **Just deploy my container** card. |
| A Dockerfile in a Git repo | Same, with the repo URL. The worker clones, builds for ARM, pushes to the tenancy registry (public repo `sbx/<sandbox>/app`), deploys. |
| An image in the tenancy registry | Give the full name `phx.ocir.io/<namespace>/<repo>:<tag>`; the repo must be public or the sandbox needs a pull secret. |
| Needs a database or Kafka | Add "with a database" / "with Kafka" to the request. Connection details arrive as environment variables in the container. |

Ports: whatever the container listens on is the port to give; only that port
is opened to the internet. Images must be `linux/arm64` (or multi-arch) for
the default A1 shape; the deploy path builds them that way automatically.

## 8. Chat agent (MCP)

`factory/mcp_server.py` exposes `create_sandbox`, `deploy_app`,
`list_sandboxes`, `destroy_sandbox` and `reap_sandboxes` as MCP tools.

```json
{ "mcpServers": { "sandbox-factory": {
    "command": "python", "args": ["C:/path/to/factory/mcp_server.py"] } } }
```

Then: "I need Kafka, an ATP and my app from ./api for three days."

## Moving to another tenancy

1. `cp foundation/personal.tfvars foundation/liberty-dev.tfvars`, change the OCIDs, budget and email.
2. `terraform apply -var-file=liberty-dev.tfvars` in `foundation/`.
3. Delete `factory/foundation.json`; the factory regenerates it from the new outputs.

Per-team isolation, SSO groups and approval flows are IAM policies on top of
the same compartments. Nothing in the stacks changes.

## Costs on a personal tenancy

Foundation: $0. A sandbox with a free ADB, one Streaming pool and one 1-OCPU
A1 container costs cents per day, dominated by the container instance.
A managed Kafka cluster or a paid ADB is the only way to spend real money;
both are behind explicit flags, and the quotas cap them.
