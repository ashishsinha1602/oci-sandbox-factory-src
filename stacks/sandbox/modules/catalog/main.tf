terraform {
  required_providers {
    oci = { source = "oracle/oci" }
  }
}

variable "compartment_id" { type = string }
variable "name" { type = string }
variable "enabled" { type = bool }
variable "data_assets" {
  description = <<-EOT
    Sources to register in the catalog. An asset is a pointer to something the
    catalog can harvest - an Object Storage bucket, an Autonomous Database -
    which is how a Spark job discovers what it is allowed to read.
  EOT
  type = list(object({
    name        = string
    type_key    = string
    description = optional(string, "")
    properties  = optional(map(string), {})
  }))
  default = []
}
variable "defined_tags" { type = map(string) }
variable "freeform_tags" { type = map(string) }
# When data_assets is empty, every bucket of the sandbox is registered as an
# Object Storage asset with a resource-principal connection, so the catalog is
# never an empty shell. The worker then asks the catalog to harvest them.
variable "region" { type = string }
variable "namespace" {
  type    = string
  default = ""
}
variable "buckets" {
  description = "Bucket names of this sandbox, registered by default."
  type        = list(string)
  default     = []
}

# OCI Data Catalog is the counterpart to the AWS Glue Data Catalog: the
# metastore that says what data exists and where. Data Flow jobs read it to
# resolve a table name to a location and schema, which is the part that turns a
# pile of files in a bucket into something a Spark job can be pointed at.
resource "oci_datacatalog_catalog" "this" {
  count          = var.enabled ? 1 : 0
  compartment_id = var.compartment_id
  display_name   = "${var.name}-catalog"
  defined_tags   = var.defined_tags
  freeform_tags  = var.freeform_tags
}

# Type keys are per catalog and must be looked up. "Resource Principal" exists
# for several asset types; the one whose parent is the Object Storage asset type
# is the right one.
data "oci_datacatalog_catalog_types" "object_storage" {
  count         = var.enabled ? 1 : 0
  catalog_id    = oci_datacatalog_catalog.this[0].id
  type_category = "dataAsset"
  name          = "Oracle Object Storage"
}
data "oci_datacatalog_catalog_types" "resource_principal" {
  count         = var.enabled ? 1 : 0
  catalog_id    = oci_datacatalog_catalog.this[0].id
  type_category = "connection"
  name          = "Resource Principal"
}
locals {
  os_type_key = var.enabled ? one([for i in data.oci_datacatalog_catalog_types.object_storage[0].type_collection[0].items : i.key]) : ""
  rp_type_key = var.enabled ? one([for i in data.oci_datacatalog_catalog_types.resource_principal[0].type_collection[0].items : i.key if i.parent_type_key == local.os_type_key]) : ""
  default_assets = var.namespace == "" ? [] : [for b in var.buckets : {
    name        = b
    type_key    = local.os_type_key
    description = "Object Storage bucket ${b} of sandbox ${var.name}"
    # the value the catalog accepts for an Object Storage asset: the Swift endpoint, no path
    properties  = { "default.url" = "https://swiftobjectstorage.${var.region}.oraclecloud.com", "default.namespace" = var.namespace }
  }]
  assets = length(var.data_assets) > 0 ? var.data_assets : local.default_assets
}

resource "oci_datacatalog_data_asset" "this" {
  for_each = var.enabled ? { for a in local.assets : a.name => a } : {}

  catalog_id   = oci_datacatalog_catalog.this[0].id
  display_name = each.value.name
  type_key     = each.value.type_key
  description  = each.value.description
  properties   = each.value.properties
}

# The catalog reads the bucket as itself (resource principal): no key stored.
resource "oci_datacatalog_connection" "rp" {
  for_each = { for k, a in oci_datacatalog_data_asset.this : k => a if a.type_key == local.os_type_key }

  catalog_id     = oci_datacatalog_catalog.this[0].id
  data_asset_key = each.value.key
  display_name   = "${each.key}-resource-principal"
  type_key       = local.rp_type_key
  is_default     = true
  properties     = { "default.ociRegion" = var.region, "default.ociCompartment" = var.compartment_id }
}

output "catalog" {
  value = var.enabled ? {
    id                = oci_datacatalog_catalog.this[0].id
    display_name      = oci_datacatalog_catalog.this[0].display_name
    number_of_objects = oci_datacatalog_catalog.this[0].number_of_objects
    service_url       = oci_datacatalog_catalog.this[0].service_api_url
    # the catalog's own console page. cloud.oracle.com/data-catalog/... is not a route (404).
    console_url       = oci_datacatalog_catalog.this[0].service_console_url
    # what is registered, with the keys the worker needs to start a harvest
    assets = [for k, a in oci_datacatalog_data_asset.this : {
      name           = k
      key            = a.key
      connection_key = try(oci_datacatalog_connection.rp[k].key, null)
    }]
  } : null
}

output "data_assets" {
  value = [for k, a in oci_datacatalog_data_asset.this : { name = k, key = a.key }]
}
