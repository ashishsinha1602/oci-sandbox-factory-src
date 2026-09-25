# How the Sandbox Factory works

A self-service playground for OCI: a user asks for infrastructure in plain
English, attaches code, or picks a starter; the factory prices it, builds it
with Terraform, hands back URLs and credentials on a card, and destroys it when
its time is up. One deployment serves a whole team; every user sees only their
own sandboxes.

## 1. The pieces

```
tenancy root ─ policies for the two identities below and for OCI services
 └─ sbx
     ├─ sbx-control  ...............  the factory itself (no user access)
     │    APEX app + control DB        the UI, the assistant, the request queue
     │    workers (container instances) 6 identical containers, each takes one request at a time
     │    VCN (public + private subnet) shared by every sandbox
     │    Vault + key                  per-sandbox secrets (Kafka superuser)
     │    Resource Manager stacks      one stack per sandbox, state kept by OCI
     │    OCIR                         images the factory builds (kaniko)
     └─ sbx-sandboxes  ..............  everything users get, tagged owner / sandbox id / expiry
          databases, buckets, queues, NoSQL, Kafka clusters, functions,
          Data Flow jobs, catalogs, container instances, API gateways
```

A **sandbox** is not a container. It is one Resource Manager stack — one
request — and it can hold any mix of the services above. A sandbox may have no
container at all (a bucket + Spark job + catalog), one container instance
(Airflow), or several (a UI and an MCP server side by side, or extra
`app_instances`).

## 2. From a sentence to running infrastructure

```
 user (APEX)            control DB              worker container            OCI
 ───────────            ──────────              ────────────────            ───
 1. types a request  ─► SF ajax process ─► Generative AI (Gemini) plans it:
    or attaches code     reads prompts,        what to build, cost table, questions
    or clicks a starter  calls the model   ◄─  JSON: services + workload
 2. clicks Create    ─► INSERT sandbox_requests (owner = APP_USER, quota + name checks in a trigger)
                                              3. claim():  SELECT ... FOR UPDATE SKIP LOCKED
                                              4. expand starters / seed SQL / app files
                                              5. build images if needed ─────────────► kaniko in a
                                                                                       container instance → OCIR
                                              6. zip stacks/sandbox + variables ─────► Resource Manager stack
                                                 APPLY job (Terraform)                 creates every resource
                                              7. post-apply, as the worker:
                                                 seed SQL, Select AI, REST, read the
                                                 Kafka secret, generated passwords
                                              8. UPDATE request: DONE + outputs (URLs, credentials, ids)
 9. card + landing page ◄─ status ajax (only rows where requester = APP_USER)
10. reaper (every 30 min, in the worker): expiry tag passed → DESTROY job → History
```

Everything a user can click comes from step 8's outputs: app URL (API
Gateway HTTPS hostname, or a public IP when the region's gateway limit is
used up), SQL Developer Web + ADMIN password, Airflow login, Kafka public
bootstrap + superuser, function URLs, and deep links into the OCI console for
buckets, Spark runs, catalogs and NoSQL tables.

## 3. How a container instance inside a sandbox comes to be

For a request with an app (an image, a Git URL, an attached folder, or a
starter):

1. The worker builds the image inside OCI when there is source (kaniko in a
   short-lived container instance in `sbx-control`, pushed to OCIR with a
   unique tag), or uses the public image named.
2. Terraform (`stacks/sandbox/modules/app`) creates in `sbx-sandboxes`:
   a network security group opening only the declared ports, one **container
   instance** (`CI.Standard.A1.Flex`, 1 OCPU / 4 GB by default) running every
   container of the request in one network namespace, and an **API Gateway**
   with a route per container (`/` and `/<name>`).
3. Every container receives the sandbox's connection details as environment
   variables (`ADB_CONNECT_STRING`, `ADB_ADMIN_PASSWORD`, `KAFKA_BOOTSTRAP_SERVERS`,
   `SANDBOX_COMPARTMENT_OCID`, …) plus anything the request set; a value of
   `{{GENERATE_PASSWORD}}` is minted by the worker and shown on the card.
4. The container authenticates to OCI as **itself** (resource principal),
   with the sandbox dynamic group's rights below — no key is ever put inside.

Extra `app_instances` in the request become additional container instances in
the same sandbox; a paid database puts the instance in the private subnet
next to the database's private endpoint.

## 4. Identities and what each may do

| Identity | Who | Rights (all scoped to `sbx-*`) |
|---|---|---|
| Installer (human, once) | tenancy admin | create compartments, policies, dynamic groups, quotas, vault, VCN; run the three Terraform stacks; import the APEX app |
| `sbx-worker-dg` (container instances in `sbx-control`) | the workers | manage all-resources in `sbx-sandboxes`; manage stacks/jobs, repos, container instances, network, objects in `sbx-control`; use the vault and keys; read limits; inspect compartments |
| `sbx-sandbox-adb-dg` (any resource in `sbx-sandboxes`) | databases, containers, functions **inside a sandbox** | use Generative AI; manage object storage, Data Flow and Data Catalog **in `sbx-sandboxes` only** |
| `sbx-control-adb-dg` | the control database | use Generative AI (the assistant) |
| OCI services | API Gateway, Functions, Data Flow, Data Catalog, Kafka | attach to the VCN, pull images, read/write sandbox buckets, write the Kafka superuser secret |
| End user | APEX account | nothing in OCI IAM; sees and controls only rows where `requester = APP_USER` |

The exact statements are in `docs/PREREQUISITES.md` and in `foundation/`.
No policy grants a human anything at run time, and none reaches outside the
`sbx` tree.

## 5. Safety rails

- **Ownership** is enforced in the database (a trigger on `sandbox_requests`), not only in the page: a user cannot destroy, retry or extend someone else's sandbox, or reuse its name.
- **Lifetime** 1–30 days is stamped on every resource as a tag; the reaper destroys the whole stack, and Object Storage buckets are emptied first.
- **Quotas** on `sbx` cap cores, memory, database ECPUs, Kafka partitions; GPUs and dedicated databases are zeroed. **Limits** the tenancy must raise for a team: API Gateway count, Data Catalog count.
- **Network**: only API Gateways (HTTPS) and the Kafka public add-on (SASL, allowed CIDRs) are public; databases, containers and brokers sit in the private subnet. Every container port is opened only to `allowed_cidr`.
- **Parallelism**: six workers claim requests with `FOR UPDATE SKIP LOCKED`; Resource Manager throttling re-queues a request instead of failing it.

## 6. Proof

`tests/e2e.py` builds one sandbox per catalogue path in parallel, verifies
each with real calls (HTTP, object put/get, queue message, NoSQL row, Data Flow
run, function URL, Select AI, ORDS REST, knowledge-base answer and its cache,
Airflow REST, Kafka produce/consume), checks the assistant and per-user
isolation, destroys everything and checks nothing is left. Reports are kept in
`tests/reports/`.
