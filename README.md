# OCI Sandbox Factory

Self-service, auto-expiring sandboxes on Oracle Cloud. A user picks what they
need (Autonomous Database, Kafka, a containerised app), gets an isolated
compartment with those pieces, and the sandbox destroys itself after N days.

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
       └── sbx-<id>             ← one compartment per sandbox
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
| `examples/hello-app/` | Sample Dockerfile for `deploy` |

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

## 5. Chat agent

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
