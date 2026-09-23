# One sandbox = one compartment under sbx-sandboxes + whatever the requester
# switched on. Everything carries the four sbx.* tags; the reaper reads
# sbx.expires and destroys the stack after that date.

resource "time_static" "created" {}

locals {
  expires = formatdate("YYYY-MM-DD", timeadd(time_static.created.rfc3339, "${var.ttl_days * 24}h"))
  name    = "sbx-${var.sandbox_id}"

  defined_tags = {
    "${var.tag_namespace}.owner"      = var.owner
    "${var.tag_namespace}.sandbox_id" = var.sandbox_id
    "${var.tag_namespace}.team"       = var.team
    "${var.tag_namespace}.expires"    = local.expires
  }

  freeform_tags = {
    managed_by = "terraform"
    stack      = "sandbox"
    sandbox_id = var.sandbox_id
  }
}

# By default every sandbox lives in the shared sbx-sandboxes compartment
# (resources are told apart by name and by the sbx.* tags). Set
# per_sandbox_compartment = true to give each sandbox its own compartment.
resource "oci_identity_compartment" "sandbox" {
  count          = var.per_sandbox_compartment ? 1 : 0
  compartment_id = var.sandboxes_compartment_ocid
  name           = local.name
  description    = "Sandbox ${var.sandbox_id} for ${var.owner}. Expires ${local.expires}."
  enable_delete  = true
  defined_tags   = local.defined_tags
  freeform_tags  = local.freeform_tags
}

# A new compartment takes a little while to become visible to every service.
resource "time_sleep" "iam_propagation" {
  depends_on      = [oci_identity_compartment.sandbox]
  create_duration = var.per_sandbox_compartment ? "60s" : "1s"
}

locals {
  compartment_id = var.per_sandbox_compartment ? oci_identity_compartment.sandbox[0].id : var.sandboxes_compartment_ocid
}

module "adb" {
  count  = var.enable_adb ? 1 : 0
  source = "./modules/adb"

  compartment_id = local.compartment_id
  name           = local.name
  tier           = var.adb_tier
  workload       = var.adb_workload
  ecpu_count     = var.adb_ecpu_count
  storage_gb     = var.adb_storage_gb
  vcn_id         = var.vcn_id
  subnet_id      = var.private_subnet_id
  allowed_cidrs  = [var.allowed_cidr]
  defined_tags   = local.defined_tags
  freeform_tags  = local.freeform_tags

  depends_on = [time_sleep.iam_propagation]
}

module "kafka" {
  count  = var.enable_kafka ? 1 : 0
  source = "./modules/kafka"

  compartment_id = local.compartment_id
  name           = local.name
  mode           = var.kafka_mode
  topics         = var.kafka_topics
  partitions     = var.kafka_partitions
  kafka_version  = var.kafka_version
  subnet_id      = var.private_subnet_id
  defined_tags   = local.defined_tags
  freeform_tags  = local.freeform_tags

  depends_on = [time_sleep.iam_propagation]
}

# Connection details of the other pieces are injected into every container.
locals {
  injected_env = merge(
    { SANDBOX_ID = var.sandbox_id, SANDBOX_EXPIRES = local.expires },
    var.enable_adb ? {
      ADB_DB_NAME        = module.adb[0].db_name
      ADB_CONNECT_STRING = module.adb[0].connect_string
      ADB_ADMIN_PASSWORD = module.adb[0].admin_password
    } : {},
    var.enable_kafka ? {
      KAFKA_BOOTSTRAP_SERVERS = module.kafka[0].bootstrap_servers
    } : {},
  )

  app_containers = [
    for c in var.app_containers : merge(c, { env = merge(local.injected_env, c.env) })
  ]
}

module "app" {
  count  = var.enable_app ? 1 : 0
  source = "./modules/app"

  compartment_id   = local.compartment_id
  name             = local.name
  vcn_id           = var.vcn_id
  subnet_id        = var.app_public ? var.public_subnet_id : var.private_subnet_id
  public           = var.app_public
  allowed_cidr     = var.allowed_cidr
  containers       = local.app_containers
  shape            = var.app_shape
  ocpus            = var.app_ocpus
  memory_gb        = var.app_memory_gb
  gateway          = var.app_gateway
  public_subnet_id = var.public_subnet_id
  defined_tags     = local.defined_tags
  freeform_tags    = local.freeform_tags

  depends_on = [time_sleep.iam_propagation]
}
