output "worker" {
  value = {
    id         = oci_container_instances_container_instance.worker.id
    name       = oci_container_instances_container_instance.worker.display_name
    state      = oci_container_instances_container_instance.worker.state
    image      = var.worker_image
    private_ip = try(oci_container_instances_container_instance.worker.vnics[0].private_ip, null)
  }
}

output "dynamic_group" {
  value       = var.create_iam ? oci_identity_dynamic_group.worker[0].name : null
  description = "Grant this principal the rights in main.tf by hand if create_iam = false."
}

output "logs_hint" {
  value = "oci container-instances container list --container-instance-id ${oci_container_instances_container_instance.worker.id} --query 'data.items[].{id:id,state:\"lifecycle-state\"}' --output table"
}
