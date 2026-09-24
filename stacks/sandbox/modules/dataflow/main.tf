terraform {
  required_providers {
    oci = { source = "oracle/oci" }
  }
}

variable "compartment_id" { type = string }
variable "name" { type = string }
variable "jobs" {
  description = <<-EOT
    Spark applications. This is OCI's equivalent of an AWS Glue ETL job: managed
    Spark, billed per run, nothing provisioned between runs. file_uri points at
    the job in Object Storage, e.g. oci://bucket@namespace/etl.py
  EOT
  type = list(object({
    name            = string
    file_uri        = string
    language        = optional(string, "PYTHON")
    spark_version   = optional(string, "3.5.0")
    driver_shape    = optional(string, "VM.Standard.E4.Flex")
    executor_shape  = optional(string, "VM.Standard.E4.Flex")
    num_executors   = optional(number, 1)
    ocpus           = optional(number, 1)
    memory_gb       = optional(number, 16)
    arguments       = optional(list(string), [])
    warehouse_uri   = optional(string)
  }))
}
variable "logs_bucket_uri" {
  description = "Where Data Flow writes run logs, e.g. oci://bucket@namespace/"
  type        = string
}
variable "injected_env" {
  type    = map(string)
  default = {}
}
variable "defined_tags" { type = map(string) }
variable "freeform_tags" { type = map(string) }

# A Data Flow application is a job definition, not a running cluster. Spark is
# started for a run and torn down afterwards, so an idle job costs nothing and
# consumes no quota - the same reason Functions suit a sandbox.
resource "oci_dataflow_application" "this" {
  for_each = { for j in var.jobs : j.name => j }

  compartment_id = var.compartment_id
  display_name   = "${var.name}-${each.value.name}"
  file_uri       = each.value.file_uri
  language       = each.value.language
  spark_version  = each.value.spark_version
  num_executors  = each.value.num_executors
  driver_shape   = each.value.driver_shape
  executor_shape = each.value.executor_shape
  arguments      = each.value.arguments
  logs_bucket_uri = var.logs_bucket_uri
  warehouse_bucket_uri = each.value.warehouse_uri
  configuration  = var.injected_env
  defined_tags   = var.defined_tags
  freeform_tags  = var.freeform_tags

  driver_shape_config {
    ocpus         = each.value.ocpus
    memory_in_gbs = each.value.memory_gb
  }

  executor_shape_config {
    ocpus         = each.value.ocpus
    memory_in_gbs = each.value.memory_gb
  }
}

output "jobs" {
  value = [
    for k, a in oci_dataflow_application.this : {
      name     = a.display_name
      id       = a.id
      file_uri = a.file_uri
      language = a.language
    }
  ]
}
