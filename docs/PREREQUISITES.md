# Installing the Sandbox Factory in an OCI tenancy

The factory is three Terraform stacks and one APEX application. Everything it
creates lives under one compartment tree, and every grant it needs is scoped
to that tree. This page lists exactly what an installer needs and what a user
needs, so it can be reviewed before anything is created.

```
tenancy
 └─ sbx                      (root of everything the product owns)
     ├─ sbx-control          VCN, workers, control database + APEX, vault, stacks
     └─ sbx-sandboxes        every user sandbox, tagged with owner and expiry
```

## 1. Who installs, and with what

| Step | Needs | Why |
|---|---|---|
| `foundation/` (`terraform apply`) | a user in a group that may **manage compartments, policies, dynamic-groups, quotas, tag-namespaces in tenancy** and **manage all-resources in compartment sbx** (once it exists). In practice: a tenancy administrator, once. | It creates the compartments, the VCN, the tag namespace, the budget and quotas, the vault, the control database, and the policies below. The policies must live in the tenancy root because service principals and dynamic groups are granted there. |
| `stacks/worker/` | same user | The parallel workers (container instances) and their dynamic group. |
| APEX app + prompts (`factory/apex_customize.py`, `factory/prompts.py`) | the control database ADMIN password (an output of foundation) | Imports application 112 into workspace SBX and loads the assistant's prompts. |
| Region | one region; the factory follows the tenancy's home region setting in `tfvars` | |

After installation no human credential is used at run time: workers act as their
own principal, sandbox code acts as its own principal, and the APEX application
runs inside the control database.

## 2. Policies the installer creates (all in the tenancy root, all scoped to `sbx-*`)

Reviewed verbatim from a live install. `<sbx-control>` / `<sbx-sandboxes>` are the compartment OCIDs.

**Workers** (dynamic group `sbx-worker-dg`: container instances in `sbx-control`)
```
allow dynamic-group sbx-worker-dg to manage all-resources in compartment <sbx-sandboxes>
allow dynamic-group sbx-worker-dg to manage orm-stacks in compartment <sbx-control>
allow dynamic-group sbx-worker-dg to manage orm-jobs in compartment <sbx-control>
allow dynamic-group sbx-worker-dg to manage repos in compartment <sbx-control>
allow dynamic-group sbx-worker-dg to manage compute-container-family in compartment <sbx-control>
allow dynamic-group sbx-worker-dg to manage virtual-network-family in compartment <sbx-control>
allow dynamic-group sbx-worker-dg to manage object-family in compartment <sbx-control>
allow dynamic-group sbx-worker-dg to manage object-family in compartment <sbx-sandboxes>
allow dynamic-group sbx-worker-dg to use vaults in compartment <sbx-control>
allow dynamic-group sbx-worker-dg to use keys in compartment <sbx-control>
allow dynamic-group sbx-worker-dg to read objectstorage-namespaces in tenancy
allow dynamic-group sbx-worker-dg to inspect compartments in tenancy
allow dynamic-group sbx-worker-dg to inspect tenancies in tenancy
allow dynamic-group sbx-worker-dg to use tag-namespaces in tenancy
```

**Code running inside a sandbox** (dynamic group `sbx-sandbox-adb-dg`: `Any {resource.compartment.id = <sbx-sandboxes>}`)
```
allow dynamic-group sbx-sandbox-adb-dg to use generative-ai-family in tenancy
allow dynamic-group sbx-sandbox-adb-dg to manage object-family in compartment <sbx-sandboxes>
allow dynamic-group sbx-sandbox-adb-dg to manage dataflow-family in compartment <sbx-sandboxes>
allow dynamic-group sbx-sandbox-adb-dg to manage data-catalog-family in compartment <sbx-sandboxes>
allow dynamic-group sbx-sandbox-adb-dg to read compartments in tenancy
```

**The control database's assistant** (dynamic group `sbx-control-adb-dg`: the control ADB)
```
allow dynamic-group sbx-control-adb-dg to manage generative-ai-family in tenancy
```

**Service principals**
```
allow service apigateway to use virtual-network-family in tenancy
allow any-user to use functions-family in compartment <sbx-sandboxes> where ALL {request.principal.type = 'ApiGateway', request.resource.compartment.id = '<sbx-sandboxes>'}
allow service dataflow to read buckets in compartment <sbx-control>
allow service dataflow to read objects in compartment <sbx-control>
allow service dataflow to read buckets in compartment <sbx-sandboxes>
allow service dataflow to manage objects in compartment <sbx-sandboxes>
allow any-user to manage object-family in compartment <sbx-sandboxes> where ALL {request.principal.type = 'dataflowrun', request.principal.compartment.id = '<sbx-sandboxes>'}
allow any-user to read object-family in compartment <sbx-sandboxes> where ALL {request.principal.type = 'datacatalog', request.principal.compartment.id = '<sbx-sandboxes>'}
allow any-user to read buckets in compartment <sbx-sandboxes> where ALL {request.principal.type = 'datacatalog', request.principal.compartment.id = '<sbx-sandboxes>'}
allow service faas to read repos in tenancy
allow service faas to use apm-domains in tenancy
allow service rawfka to {SECRET_UPDATE} in compartment <sbx-sandboxes>
allow service rawfka to use secrets in compartment <sbx-sandboxes> where request.operation = 'UpdateSecret'
allow service rawfka to read secrets in compartment <sbx-sandboxes>
allow service rawfka to use virtual-network-family in compartment <sbx-control>
allow service rawfka to use virtual-network-family in compartment <sbx-sandboxes>
```

Nothing grants anything outside `sbx-*`, and no policy grants a human user anything.

## 3. Service limits that decide how many sandboxes can run at once

These are per-region tenancy limits (Console > Governance > Limits). The defaults of a new tenancy are small; raise them before a team uses the factory:

| Limit | Default | What it caps | Recommended |
|---|---|---|---|
| API Gateway `gateway-count` | 5 | one gateway per sandbox with an app, plus one per sandbox with functions. When it is used up the factory falls back to a **public IP** (HTTP, no Oracle hostname) and says so on the card | 50 |
| Data Catalog `catalog-count` | 2 | one catalog per sandbox that asks for one | 10 |
| Autonomous Database `atp-ecpu-count` | varies | 2 ECPU per paid database | 64 |
| Container Instances A1 cores / memory | varies | 1 core, 4 GB per app container by default | 64 / 1 TB |
| Streaming with Apache Kafka clusters | varies | one per Kafka sandbox | 5 |

## 3a. Quotas and limits the tenancy must have

The foundation sets **quotas** on `sbx` so the playground cannot outgrow its budget
(defaults: 64 A1 cores / 1 TB memory, 64 ATP ECPUs + 16 ADW ECPUs, 2 OKE
clusters, 50 streaming partitions, GPUs and dedicated/ExaCC databases zeroed).
The tenancy's **service limits** must be at least that high in the region, and
these services must be available there: Container Instances (A1), Autonomous
Database (23ai, ECPU), Streaming with Apache Kafka, NoSQL, Functions, API
Gateway, Data Flow, Data Catalog, Vault, Resource Manager, Generative AI
(on-demand chat models), OCIR. Generative AI is the one most often missing in
a region: the assistant and the knowledge-base starter need it.

## 4. What a user needs

| To | Needs |
|---|---|
| Use the factory (chat, starters, build form, cards, landing pages, destroy) | an APEX account in workspace SBX (`python factory/controldb.py users alice@example.com`), nothing in OCI IAM. Users see and control only their own sandboxes. |
| Use what they built | the URLs and credentials on their card: app URL, SQL Developer Web + ADMIN password, Airflow login, Kafka bootstrap + superuser, function URLs. No OCI login. |
| Open a resource in the OCI console (Data Flow runs, catalog, NoSQL Table Explorer, buckets) | an OCI user in a group with `read all-resources in compartment sbx-sandboxes` (optional; only for the console links). |
| Have their code deployed | a public Git URL, or a folder attached in the chat. The factory builds inside OCI; nothing is pulled from the user's machine. |

## 5. Install order

```
cd foundation && terraform init && terraform apply -var-file=<tenancy>.tfvars      # compartments, VCN, quotas, vault, control DB, policies
cd ../stacks/worker && terraform apply -var-file=<tenancy>.tfvars                  # workers
cd ../../factory
python controldb.py setup            # schema, tables, triggers, APEX workspace
python apex_customize.py             # the application
python prompts.py                    # the assistant's prompts
python controldb.py users you@example.com
python ../tests/e2e.py               # proves every path in this tenancy, then destroys what it made
```

## 6. Known limits (honest list)

- Isolation between users is at the application and network layer; inside OCI, all sandboxes share one compartment and one dynamic group. `per_sandbox_compartment = true` gives IAM-level isolation at the cost of slower creates.
- Generated passwords are stored in the control database and shown on the owner's card. Move them to Vault before a wide rollout.
- Users are APEX accounts; SSO (OCI IAM / IDCS) is an APEX authentication-scheme change, not yet done.
- The build context for "attach a folder" is flat (no subdirectories); a Git URL has no such limit.
- One region per install.
