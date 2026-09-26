# Making Sandbox Factory install in any OCI tenancy

The goal: someone with an OCI tenancy clicks **Deploy to Oracle Cloud**, answers a
few questions, and gets a working factory that passes the full test suite, in any
region, without editing code. We prove it on our own tenancy first (tear down,
install from Terraform only, run the suite), then on a tenancy that is not ours.

Status keys: **done** (merged and verified), **doing**, **next**.

## 1. Nothing about a tenancy or region in the code — done

| Pin | Fix | Proof |
|---|---|---|
| Worker image path (`phx.ocir.io/<our namespace>/...`) in `roll_spec.yaml` | `roll_workers.py --tag` reuses the repository the workers already run | pipeline roll |
| Region `us-phoenix-1` as a fallback in the chat back end, the page and the worker | Everything reads the **tenancy profile** (below); no region fallback | `profile.py --force` |
| Test function image registry `phx.ocir.io` | `tests/e2e.py` derives `<region key>.ocir.io/<namespace>` from the profile | e2e build check |
| Fixed list of six chat models in the page | The profile probes every candidate; the picker and the back end offer only models that answer here | 3 of 8 answer in us-phoenix-1 |
| Select AI / starter apps default to Gemini in Phoenix | Worker and sandboxes get `genai_model` and `OCI_REGION` from the profile (`CHAT_MODEL` injected into every container) | sandbox env |

**Tenancy profile** (`factory/profile.py`, refreshed by every worker at start):
region, region key, Object Storage namespace, registry prefix, Generative AI
region, the chat models that answer on demand here (probed with a one-line call)
and the default model. Saved in `SBX.FACTORY_CONFIG`.

## 2. Everything the install needs is Terraform — next

Created by hand today, to move into `foundation/`:

- [ ] OCI DevOps project, notification topic, code repository (mirror of the public repo)
- [ ] Build pipeline (build → deliver → roll → e2e), deliver artifact, trigger with `factory/**`-style globs
- [ ] OCI Logging log group + DevOps service log (runs fail without it)
- [ ] OCIR auth token for kaniko pushes (Terraform `oci_identity_auth_token`, or document the one manual step)
- [ ] First end-user accounts (APEX) — `controldb.py users` run by the worker from a variable
- [ ] The worker image: published publicly once per release so a new tenancy pulls it before it can build its own

## 3. One-click install — next

- [ ] Resource Manager stack zip (`release.py`) with `schema.yaml` asking only: region, admin email, budget, first users
- [ ] "Deploy to Oracle Cloud" button in the README
- [ ] Install order handled by the stack (foundation → worker → bootstrap), no laptop step

## 4. Open product items — next

- [ ] Kafka public endpoint: install the add-on through the SDK in the worker, not the Terraform provider (fails even after cleanup + re-apply)
- [ ] Cost estimates computed from the live price list, not model arithmetic ($1,189 vs $115 for the same database)
- [ ] Data Catalog harvest (service NPE; needs the entity-selection API form)

## 5. Proof — next

- [ ] Tear down our install completely and reinstall from Terraform only; full e2e suite green
- [ ] Install in a second region (or tenancy) that has never run the factory; full e2e suite green
- [ ] Record the minimum privileges that install actually used (docs/PREREQUISITES.md)
