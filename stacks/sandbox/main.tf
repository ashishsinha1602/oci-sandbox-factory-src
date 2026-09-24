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
  db_version     = var.adb_version
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

module "nosql" {
  count  = var.enable_nosql ? 1 : 0
  source = "./modules/nosql"

  compartment_id = local.compartment_id
  name           = local.name
  tables         = length(var.nosql_tables) > 0 ? var.nosql_tables : local.default_nosql_tables
  defined_tags   = local.defined_tags
  freeform_tags  = local.freeform_tags

  depends_on = [time_sleep.iam_propagation]
}

# A sandbox asking for NoSQL with nothing specified still gets something to use.
locals {
  default_nosql_tables = [{
    name    = "events"
    ddl     = "CREATE TABLE IF NOT EXISTS {name} (id STRING, created_at TIMESTAMP(3), kind STRING, payload JSON, PRIMARY KEY (id))"
    read    = 5
    write   = 5
    storage = 1
  }]
}

# Additional databases. The first one stays module.adb so every existing output and
# injected variable keeps its meaning; these are siblings with their own credentials.
module "adb_extra" {
  for_each = { for d in var.adb_databases : d.name => d }
  source   = "./modules/adb"

  compartment_id = local.compartment_id
  name           = "${local.name}-${each.value.name}"
  tier           = each.value.tier
  workload       = each.value.workload
  db_version     = var.adb_version
  ecpu_count     = each.value.ecpu_count
  storage_gb     = each.value.storage_gb
  vcn_id         = var.vcn_id
  subnet_id      = var.private_subnet_id
  allowed_cidrs  = [var.allowed_cidr]
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
    # Each extra database is injected as ADB_<NAME>_CONNECT_STRING / _ADMIN_PASSWORD,
    # so an app can talk to several of them at once.
    merge([for k, m in module.adb_extra : {
      "ADB_${upper(replace(k, "-", "_"))}_CONNECT_STRING" = m.connect_string
      "ADB_${upper(replace(k, "-", "_"))}_ADMIN_PASSWORD" = m.admin_password
      "ADB_${upper(replace(k, "-", "_"))}_DB_NAME"        = m.db_name
    }]...),
    var.enable_nosql ? {
      NOSQL_COMPARTMENT_OCID = local.compartment_id
      NOSQL_TABLES           = join(",", module.nosql[0].tables)
      NOSQL_REGION           = var.region
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
