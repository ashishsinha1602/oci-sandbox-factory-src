"""Replace every worker with a new image.

    python roll_workers.py <image>

Deletes the running worker container instances and creates the same number
from the image given, with the environment the old ones had. Workers finish
the request they hold before the old instance disappears only if the queue
is drained first; run this between requests or accept one retried request.
"""
import sandbox_factory as sf, oci, time
import os, sys
IMAGE=sys.argv[1] if len(sys.argv)>1 else os.environ["WORKER_IMAGE"]; N=int(os.environ.get("WORKER_COUNT","6"))
fnd=sf.foundation(); auth=sf.auth(); cfg=sf.config()
cc=oci.container_instances.ContainerInstanceClient(**auth); idc=oci.identity.IdentityClient(**auth)
ctrl=fnd["compartments"]["control"]
ws=[x for x in cc.list_container_instances(compartment_id=ctrl).data.items
    if "worker" in x.display_name.lower() and x.lifecycle_state=="ACTIVE"]
env=None
for c in ws:
    for k in cc.get_container_instance(c.id).data.containers:
        env=cc.get_container(k.container_id).data.environment_variables
for c in ws: cc.delete_container_instance(c.id)
for i in range(30):
    if not [x for x in cc.list_container_instances(compartment_id=ctrl).data.items
            if "worker" in x.display_name.lower() and x.lifecycle_state not in ("DELETED",)]: break
    time.sleep(10)
m=oci.container_instances.models
ad=idc.list_availability_domains(compartment_id=cfg["tenancy"]).data[0].name
made=[]
for n in range(1,N+1):
    nm="sbx-worker" if n==1 else f"sbx-worker-{n}"
    made.append(cc.create_container_instance(m.CreateContainerInstanceDetails(
      compartment_id=ctrl, availability_domain=ad, display_name=nm, shape="CI.Standard.A1.Flex",
      shape_config=m.CreateContainerInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=4),
      container_restart_policy="ALWAYS", freeform_tags={"managed_by":"sandbox-factory","role":"worker"},
      vnics=[m.CreateContainerVnicDetails(subnet_id=fnd["network"]["private_subnet_id"],
             display_name=nm, is_public_ip_assigned=False)],
      containers=[m.CreateContainerDetails(display_name="worker", image_url=IMAGE,
             environment_variables=env)])).data.id)
up=0
for i in made:
    for _ in range(45):
        st=cc.get_container_instance(i).data.lifecycle_state
        if st in ("ACTIVE","FAILED"): up+=(st=="ACTIVE"); break
        time.sleep(10)
print(f"{up} of {N} workers ACTIVE on {IMAGE.split(':')[-1]}")
