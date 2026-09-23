# Demo runbook: deploy an app with a database, use it, destroy it

Everything below runs from your laptop against your personal tenancy. All
resources are created by one Terraform stack in OCI Resource Manager and
removed the same way.

## 0. One-time checks (already done on this laptop)

| Item | Where |
|---|---|
| OCI CLI config + API key | `~/.oci/config` (profile DEFAULT, region us-phoenix-1) |
| Terraform 1.16 | `~/bin/terraform.exe` (add `C:\Users\sinha\bin` to PATH, or the scripts find it) |
| Docker Desktop with buildx | running |
| Registry auth token | `~/.oci/ocir_token` |
| Foundation stack | applied: compartments, VCN, budget, quotas, control ATP |
| APEX app | https://g63702d3fbc0a2d-sbxctl.adb.us-phoenix-1.oraclecloudapps.com/ords/r/sbx/sandbox-factory |
| Worker | Windows scheduled task `SandboxFactory\Worker` (restarts within 10 min if it dies); reaper task `SandboxFactory\Reap` daily 09:00 |

Limits to remember: Always Free allows **2 Autonomous Databases per tenancy**.
The control database uses one, so **only one sandbox with a free ADB can
exist at a time**. Destroy it before creating the next, or use the paid tier.

## 1. Start (if the worker is not running)

```powershell
schtasks /Run /TN "SandboxFactory\Worker"
```

or in a terminal you keep open:

```powershell
cd C:\Users\sinha\Documents\oci-sandbox-factory\factory
python worker.py
```

## 2. Deploy the demo app with a database

Option A, from the APEX app (what the audience sees):

1. Open the app URL, sign in (`SBX` / your password).
2. Card **Chat with the factory**. Type:
   `deploy C:SERSSINHADOCUMENTSOCI-SANDBOX-FACTORYEXAMPLESORDERS-APP ON PORT 8080 WITH A DATABASE FOR 2 DAYS, CALL IT ORDERS-DEMO`
   The AI proposes the action. Click **Create it**.
3. The status card shows QUEUED → RUNNING (Terraform log streams) → DONE with the app URL and the SQL Developer Web link. About 4 minutes.

Option B, from a terminal (builds the image from source first):

```powershell
cd C:\Users\sinha\Documents\oci-sandbox-factory
python factory\sandbox_factory.py deploy orders-demo .\examples\orders-app --port 8080 --adb --ttl 2 --owner you@example.com
```

This builds `examples/orders-app` for ARM, pushes it to your registry as a
public repo `sbx/orders-demo/app`, and runs the same Terraform stack.

## 3. Show that it is real

1. Open the app URL from the outputs. Add a few orders.
2. Open the SQL Developer Web link from the outputs, sign in as `ADMIN` with
   the password from the outputs (`adb_admin_password` in Resource Manager →
   the stack's job outputs; or `terraform output` locally).
3. Run `select * from orders;` — the rows you just added.
4. In the OCI console: Identity → Compartments → sbx → sbx-sandboxes → `sbx-orders-demo`
   shows the ADB, the container instance and the network security group.
   Developer Services → Resource Manager → Stacks shows `sbx-orders-demo`
   and its apply job with the full Terraform plan.

## 4. Destroy

In the chat: `destroy orders-demo` → **Destroy**. Or:

```powershell
python factory\sandbox_factory.py destroy orders-demo
```

Resource Manager runs `terraform destroy`; the compartment, database,
container and rules are gone in about 3 minutes. Without doing anything,
the reaper destroys it after the lifetime (max 3 days).

## 5. If something goes wrong

| Symptom | Cause / fix |
|---|---|
| `adb-free-count` quota exceeded | A free ADB sandbox already exists (or demo leftovers). `python factory\sandbox_factory.py list` and destroy it. |
| Request stays QUEUED | Worker not running. Step 1. |
| App URL times out | Wrong port. The port given must be the one the container listens on (orders-app: 8080). |
| `docker login` fails | Token file missing or expired: Console → Profile → Auth tokens → Generate → save to `~/.oci/ocir_token`. |
| Chat says "Generative AI returned 404" | Policy `sbx-control-adb-genai` missing or the database's identity token is stale: run `begin dbms_cloud_admin.disable_resource_principal; dbms_cloud_admin.enable_resource_principal; end;` as ADMIN. |

## 6. Cleanup after the demo

```powershell
python factory\sandbox_factory.py list
python factory\sandbox_factory.py reap        # destroys anything past its expiry
```

The foundation (compartments, VCN, control database, APEX app) stays; it
costs nothing.
