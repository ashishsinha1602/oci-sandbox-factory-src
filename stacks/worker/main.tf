# The factory's own worker, moved off the laptop.
#
# worker.py polls SBX.SANDBOX_REQUESTS in the control ATP, builds images with
# kaniko and drives Resource Manager. On a laptop it authenticates from
# ~/.oci/config; here it uses a resource principal, which is why the dynamic
# group below matters more than anything else in this file.
#
# Deliberately NOT tagged managed_by = "sandbox-factory" with an expires date:
# cmd_reap() destroys exactly those stacks once expires is in the past, and a
# worker that reaps itself takes the factory down with it.

locals {
  name = "sbx-worker"

  freeform_tags = {
    managed_by = "sandbox-factory-control"
    stack      = "worker"
  }

  # sandbox_factory.foundation() reads this shape; the key names are load-bearing
  # (compartments.control, network.vcn_id, ... - see build_variables()).
  foundation = {
    compartments = {
      control   = var.control_compartment_ocid
      sandboxes = var.sandboxes_compartment_ocid
    }
    network = {
      vcn_id            = var.vcn_id
      public_subnet_id  = var.public_subnet_id
      private_subnet_id = var.private_subnet_id
    }
    tag_namespace = var.tag_namespace
  }
}

data "oci_identity_availability_domains" "ads" {
  compartment_id = var.control_compartment_ocid
}

# --- identity: the worker acts as itself, not as a user ---------------------

# Container Instances publish a resource principal; sandbox_factory.auth() picks
# it up from OCI_RESOURCE_PRINCIPAL_VERSION, which the service injects.
resource "oci_identity_dynamic_group" "worker" {
  count = var.create_iam ? 1 : 0

  compartment_id = var.tenancy_ocid
  name           = "${local.name}-dg"
  description    = "The Sandbox Factory worker container instance in sbx-control."
  matching_rule  = "ALL {resource.type = 'computecontainerinstance', resource.compartment.id = '${var.control_compartment_ocid}'}"
  freeform_tags  = local.freeform_tags
}

resource "oci_identity_policy" "worker" {
  count = var.create_iam ? 1 : 0

  compartment_id = var.tenancy_ocid
  name           = "${local.name}-policy"
  description    = "What the Sandbox Factory worker may do."
  freeform_tags  = local.freeform_tags

  statements = [
    # Everything it builds for people lives here, including per-sandbox
    # compartments when per_sandbox_compartment = true.
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to manage all-resources in compartment id ${var.sandboxes_compartment_ocid}",

    # One Resource Manager stack per sandbox, kept in sbx-control.
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to manage orm-stacks in compartment id ${var.control_compartment_ocid}",
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to manage orm-jobs in compartment id ${var.control_compartment_ocid}",

    # ensure_public_repo() creates the OCIR repo; kaniko pushes into it.
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to manage repos in compartment id ${var.control_compartment_ocid}",

    # oci_build.build_in_oci() runs kaniko as a throwaway container instance here.
    # compute-container-family, not compute-container-instances: creating an
    # instance also creates container resources, and the narrower grant fails
    # with 404 NotAuthorizedOrNotFound on create_container_instance.
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to manage compute-container-family in compartment id ${var.control_compartment_ocid}",
    # manage, not use: a sandbox's NSGs live in sbx-sandboxes but attach to the
    # VCN in sbx-control, and a public API Gateway allocates a public IP in the
    # control compartment's subnet. "use" reads them but cannot create, which
    # surfaces as 404 NotAuthorizedOrNotFound.
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to manage virtual-network-family in compartment id ${var.control_compartment_ocid}",

    # get_namespace(), list_regions(), and the sbx.* defined tags.
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to read objectstorage-namespaces in tenancy",
    # Per-sandbox secrets (Kafka superuser) are created under the shared vault in sbx-control.
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to use vaults in compartment id ${var.compartment_ocid}",
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to use keys in compartment id ${var.compartment_ocid}",
    # The worker reads service limits to decide between an API Gateway and a public IP.
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to read limits in tenancy",
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to read repos in tenancy",
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to inspect compartments in tenancy",
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to inspect tenancies in tenancy",
    "allow dynamic-group ${oci_identity_dynamic_group.worker[0].name} to use tag-namespaces in tenancy",
  ]
}

# IAM is eventually consistent; a container instance that starts before its
# policy lands spends its first minutes throwing 404s at Resource Manager.
resource "time_sleep" "iam_propagation" {
  depends_on      = [oci_identity_policy.worker]
  create_duration = var.create_iam ? "90s" : "1s"
}

# --- network: egress only ---------------------------------------------------

# The worker serves nothing. It needs to reach OCIR, Resource Manager and the
# control ATP, all outbound through the private subnet's NAT gateway.
resource "oci_core_network_security_group" "worker" {
  compartment_id = var.control_compartment_ocid
  vcn_id         = var.vcn_id
  display_name   = local.name
  freeform_tags  = local.freeform_tags
}

resource "oci_core_network_security_group_security_rule" "egress" {
  network_security_group_id = oci_core_network_security_group.worker.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
}

# --- the worker itself ------------------------------------------------------

resource "oci_container_instances_container_instance" "worker" {
  compartment_id           = var.control_compartment_ocid
  availability_domain      = data.oci_identity_availability_domains.ads.availability_domains[0].name
  display_name             = local.name
  shape                    = var.worker_shape
  container_restart_policy = "ALWAYS"
  freeform_tags            = local.freeform_tags

  shape_config {
    ocpus         = var.worker_ocpus
    memory_in_gbs = var.worker_memory_gb
  }

  vnics {
    subnet_id             = var.private_subnet_id
    display_name          = local.name
    is_public_ip_assigned = false
    nsg_ids               = [oci_core_network_security_group.worker.id]
  }

  containers {
    display_name = "worker"
    image_url    = var.worker_image

    # SBX_WORKER_KIND=oci, SBX_BUILD_MODE=kaniko and PYTHONUNBUFFERED=1 are baked
    # into the image. These four are not, and the worker exits without them:
    # controldb.connect() and foundation() both fall back to shelling out to
    # `terraform output`, and the image carries no foundation/ directory.
    # OCIR_USER/OCIR_TOKEN are only needed for Git-URL deploys, where
    # oci_build.py runs kaniko and pushes the result. Left empty, the worker
    # still serves every request that names an image it can pull.
    environment_variables = merge({
      SBX_FOUNDATION      = jsonencode(local.foundation)
      SBX_CONTROL_CONNECT = var.control_connect_string
      SBX_ADMIN_PASSWORD  = var.control_admin_password
      SBX_OWNER           = var.owner
      }, var.ocir_token == "" ? {} : {
      OCIR_USER  = var.ocir_user
      OCIR_TOKEN = var.ocir_token
    })
  }

  depends_on = [time_sleep.iam_propagation]
}
