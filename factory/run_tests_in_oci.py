"""Run the end-to-end suite inside OCI.

    python run_tests_in_oci.py [--kafka] [--only web,data]

Starts one container instance in sbx-control from the workers' own image, with
the workers' environment and identity, running tests/e2e.py. Nothing runs on
the machine that launches it: it only starts the job, streams the log, and
prints where the report landed (bucket sbx-factory-reports). Exit code is the
suite's.
"""
import sys
import time

import oci
import sandbox_factory as sf


def main(argv):
    fnd = sf.foundation()
    auth = sf.auth()
    cc = oci.container_instances.ContainerInstanceClient(**auth)
    idc = oci.identity.IdentityClient(**auth)
    ctrl = fnd["compartments"]["control"]
    workers = [x for x in cc.list_container_instances(compartment_id=ctrl).data.items
               if "worker" in x.display_name.lower() and x.lifecycle_state == "ACTIVE"]
    if not workers:
        sys.exit("no active worker to copy the image and environment from")
    w = cc.get_container_instance(workers[0].id).data
    c = cc.get_container(w.containers[0].container_id).data
    m = oci.container_instances.models
    ad = idc.list_availability_domains(compartment_id=sf.config()["tenancy"]).data[0].name
    name = f"sbx-e2e-runner-{time.strftime('%H%M%S')}"
    # The runner is the workers' image, so it must run on the workers' shape
    # (the image is built for one architecture: E4 = x86, A1 = arm).
    inst = cc.create_container_instance(m.CreateContainerInstanceDetails(
        compartment_id=ctrl, availability_domain=ad, display_name=name, shape=w.shape,
        shape_config=m.CreateContainerInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=4),
        container_restart_policy="NEVER", freeform_tags={"managed_by": "sandbox-factory", "role": "e2e"},
        vnics=[m.CreateContainerVnicDetails(subnet_id=fnd["network"]["private_subnet_id"], display_name=name, is_public_ip_assigned=False)],
        containers=[m.CreateContainerDetails(display_name="e2e", image_url=c.image_url,
                                             environment_variables={**(c.environment_variables or {}), "OCIR_USER": (c.environment_variables or {}).get("OCIR_USER", "")},
                                             working_directory="/app", command=["python", "-u", "tests/e2e.py"] + argv)])).data
    print(f"started {name} from {c.image_url.split(':')[-1]}", flush=True)
    seen = 0
    while True:
        ci = cc.get_container_instance(inst.id).data
        try:
            log = cc.retrieve_logs(ci.containers[0].container_id).data.content.decode("utf-8", "replace")
            lines = log.splitlines()
            for line in lines[seen:]:
                print(line.split(" ", 2)[-1] if line.startswith("20") else line, flush=True)
            seen = len(lines)
        except Exception:  # noqa: BLE001
            pass
        if ci.lifecycle_state in ("INACTIVE", "FAILED", "DELETED"):
            break
        time.sleep(20)
    cont = cc.get_container(ci.containers[0].container_id).data
    code = getattr(cont, "exit_code", None)
    cc.delete_container_instance(inst.id)
    print(f"runner finished with exit code {code}; report in bucket sbx-factory-reports", flush=True)
    sys.exit(0 if code == 0 else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
