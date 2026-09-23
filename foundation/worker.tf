# The factory worker, hosted in OCI: a Container Instance in sbx-control that
# polls the request queue, builds images with kaniko, and drives Resource
# Manager. It authenticates with its resource principal (no key file).

variable "enable_worker" {
  type    = bool
  default = false
}

variable "worker_image" {
  type        = string
  default     = ""
  description = "Image in OCIR built from factory/Dockerfile, e.g. phx.ocir.io/<ns>/sbx/factory/worker:latest (public repo)."
}

variable "ocir_user" {
  type        = string
  default     = ""
  description = "Registry login user for kaniko pushes: <username> (the namespace is prepended)."
}

variable "ocir_token" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Auth token for the registry login used by kaniko."
}

resource "oci_identity_dynamic_group" "worker" {
  count          = var.enable_worker ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${var.prefix}-worker-dg"
  description    = "Sandbox factory worker and build containers."
  matching_rule  = "ALL {resource.type = 'computecontainerinstance', resource.compartment.id = '${oci_identity_compartment.control.id}'}"
  freeform_tags  = local.freeform_tags
}

resource "oci_identity_policy" "worker" {
  count          = var.enable_worker ? 1 : 0
  compartment_id = var.tenancy_ocid
  name           = "${var.prefix}-worker"
  description    = "The worker may create and destroy everything inside the sandbox tree."
  freeform_tags  = local.freeform_tags
  statements = [
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to manage all-resources in compartment ${oci_identity_compartment.root.name}",
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to use tag-namespaces in tenancy",
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to read compartments in tenancy",
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to inspect tenancies in tenancy",
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to read objectstorage-namespaces in tenancy",
  ]
}

data "oci_identity_availability_domains" "worker" {
  count          = var.enable_worker ? 1 : 0
  compartment_id = var.tenancy_ocid
}

resource "oci_container_instances_container_instance" "worker" {
  count                    = var.enable_worker ? 1 : 0
  compartment_id           = oci_identity_compartment.control.id
  availability_domain      = data.oci_identity_availability_domains.worker[0].availability_domains[0].name
  display_name             = "${var.prefix}-worker"
  shape                    = "CI.Standard.A1.Flex"
  container_restart_policy = "ALWAYS"
  freeform_tags            = local.freeform_tags

  shape_config {
    ocpus         = 1
    memory_in_gbs = 4
  }

  vnics {
    subnet_id             = oci_core_subnet.private.id
    display_name          = "${var.prefix}-worker"
    is_public_ip_assigned = false
  }

  containers {
    display_name = "worker"
    image_url    = var.worker_image
    environment_variables = {
      SBX_WORKER_KIND     = "oci"
      SBX_BUILD_MODE      = "kaniko"
      SBX_CONTROL_CONNECT = oci_database_autonomous_database.control[0].connection_strings[0].all_connection_strings["LOW"]
      SBX_ADMIN_PASSWORD  = random_password.control_adb_admin[0].result
      SBX_FOUNDATION = jsonencode({
        compartments = { root = oci_identity_compartment.root.id, control = oci_identity_compartment.control.id, sandboxes = oci_identity_compartment.sandboxes.id }
        network = {
          vcn_id         = oci_core_vcn.sandbox.id, public_subnet_id = oci_core_subnet.public.id, private_subnet_id = oci_core_subnet.private.id
          nat_gateway_id = oci_core_nat_gateway.nat.id, service_gateway_id = oci_core_service_gateway.sgw.id
        }
        tag_namespace = oci_identity_tag_namespace.sandbox.name
      })
      OCIR_USER  = var.ocir_user
      OCIR_TOKEN = var.ocir_token
    }
  }

  depends_on = [oci_identity_policy.worker]
}

output "worker" {
  value = var.enable_worker ? {
    id    = oci_container_instances_container_instance.worker[0].id
    state = oci_container_instances_container_instance.worker[0].state
  } : null
}
