"""Replace the workers with a new image, one at a time.

    python roll_workers.py <image>        # WORKER_COUNT (default 6), WORKER_SHAPE

Each worker is deleted and re-created under the same name from the image
given, with the environment the old one had, and the next is only touched
once the new one is ACTIVE - so the factory never drops to zero workers and
a bad image stops the roll at the first worker instead of taking all of
them down. A worker that comes back under its name re-queues whatever its
predecessor was running (worker.recover_own_claims), so a request caught by
the roll is retried, not stranded. Exit code 1 when any worker is not ACTIVE
at the end, which fails the pipeline stage that runs this.
"""
import os
import sys
import time

import oci
import sandbox_factory as sf

# An image path, or "--tag <tag>": the repository the workers already run with
# a new tag. The pipeline uses the tag form, so nothing about the tenancy or its
# registry is written in roll_spec.yaml.
ARG = sys.argv[1:] or [os.environ.get("WORKER_IMAGE", "")]
IMAGE = ARG[0] if ARG[0] != "--tag" else None
TAG = ARG[1] if ARG[0] == "--tag" and len(ARG) > 1 else None
N = int(os.environ.get("WORKER_COUNT", "6"))
SHAPE = os.environ.get("WORKER_SHAPE", "CI.Standard.E4.Flex")

fnd = sf.foundation()
auth = sf.auth()
cfg = sf.config()
cc = oci.container_instances.ContainerInstanceClient(**auth)
idc = oci.identity.IdentityClient(**auth)
m = oci.container_instances.models
ctrl = fnd["compartments"]["control"]
ad = idc.list_availability_domains(compartment_id=cfg["tenancy"]).data[0].name


def workers() -> dict:
    return {x.display_name: x for x in cc.list_container_instances(compartment_id=ctrl).data.items
            if "worker" in x.display_name.lower() and x.lifecycle_state not in ("DELETED",)}


def wait(instance_id: str, states: tuple, seconds: int) -> str:
    for _ in range(seconds // 10):
        st = cc.get_container_instance(instance_id).data.lifecycle_state
        if st in states:
            return st
        time.sleep(10)
    return cc.get_container_instance(instance_id).data.lifecycle_state


# the environment every worker shares, from any that is running
env = None
for x in workers().values():
    if x.lifecycle_state == "ACTIVE":
        for k in cc.get_container_instance(x.id).data.containers:
            env = cc.get_container(k.container_id).data.environment_variables
        if env:
            break
if not env:
    sys.exit("no running worker to take the environment from; create the workers with stacks/worker first")
if IMAGE is None:
    current = next(k for x in workers().values() if x.lifecycle_state == "ACTIVE"
                   for k in [cc.get_container(cc.get_container_instance(x.id).data.containers[0].container_id).data.image_url])
    IMAGE = current.rsplit(":", 1)[0] + ":" + TAG
    print(f"rolling to {IMAGE} (the workers' repository, new tag)", flush=True)

names = ["sbx-worker"] + [f"sbx-worker-{n}" for n in range(2, N + 1)]
active = 0
for nm in names:
    old = workers().get(nm)
    if old:
        cc.delete_container_instance(old.id)
        st = wait(old.id, ("DELETED",), 300)
        if st != "DELETED":
            print(f"{nm}: old instance still {st} after 5 min; stopping the roll", flush=True)
            break
    new = cc.create_container_instance(m.CreateContainerInstanceDetails(
        compartment_id=ctrl, availability_domain=ad, display_name=nm, shape=SHAPE,
        shape_config=m.CreateContainerInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=4),
        container_restart_policy="ALWAYS", freeform_tags={"managed_by": "sandbox-factory", "role": "worker"},
        vnics=[m.CreateContainerVnicDetails(subnet_id=fnd["network"]["private_subnet_id"], display_name=nm, is_public_ip_assigned=False)],
        containers=[m.CreateContainerDetails(display_name="worker", image_url=IMAGE,
                                             environment_variables={**env, "WORKER_NAME": nm})])).data
    st = wait(new.id, ("ACTIVE", "FAILED"), 600)
    if st == "ACTIVE":
        # ACTIVE only means the container started. A worker that crashes at
        # start is restarted by the policy and still shows ACTIVE, so watch it
        # for 90 s: any restart means the image is bad and the roll stops here.
        time.sleep(90)
        cid = cc.get_container_instance(new.id).data.containers[0].container_id
        restarts = cc.get_container(cid).data.container_restart_attempt_count or 0
        # A worker can also stay up and fail every poll (it logs "db error" and
        # retries), which is just as dead. Its first 90 s of log must be clean.
        try:
            early = cc.retrieve_logs(cid).data.content.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            early = ""
        if not restarts and ("db error" in early or "Traceback" in early):
            restarts = "0, but its log shows errors"
        if restarts:
            try:
                tail = cc.retrieve_logs(cid).data.content.decode("utf-8", "replace").splitlines()[-8:]
                print("\n".join("    " + ln[-200:] for ln in tail), flush=True)
            except Exception:  # noqa: BLE001
                pass
            st = f"CRASHING ({restarts} restarts)"
    print(f"{nm}: {st} on {IMAGE.split(':')[-1]}", flush=True)
    if st != "ACTIVE":
        print(f"{nm} did not come up; stopping the roll so the remaining workers keep serving", flush=True)
        break
    active += 1

print(f"{active} of {N} workers ACTIVE on {IMAGE.split(':')[-1]}", flush=True)
sys.exit(0 if active == N else 1)
