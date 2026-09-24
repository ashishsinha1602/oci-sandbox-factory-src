terraform {
  required_providers {
    oci = { source = "oracle/oci" }
  }
}

variable "compartment_id" { type = string }
variable "name" { type = string }
variable "subnet_id" { type = string }
variable "functions" {
  description = "Functions to create. Each image must already exist in a registry the tenancy can pull from."
  type = list(object({
    name        = string
    image       = string
    memory_mbs  = optional(number, 256)
    timeout_sec = optional(number, 30)
    env         = optional(map(string), {})
  }))
}
variable "injected_env" {
  description = "Connection details for the rest of the sandbox, merged into every function."
  type        = map(string)
  default     = {}
}
variable "defined_tags" { type = map(string) }
variable "freeform_tags" { type = map(string) }

# OCI Functions: serverless containers, billed per invocation rather than per
# hour. They cost nothing while idle, which is why they do not count against the
# A1 core quota the way a container instance does.
resource "oci_functions_application" "this" {
  compartment_id = var.compartment_id
  display_name   = "${var.name}-fn"
  subnet_ids     = [var.subnet_id]
  shape          = "GENERIC_ARM"
  defined_tags   = var.defined_tags
  freeform_tags  = var.freeform_tags
}

resource "oci_functions_function" "this" {
  for_each = { for f in var.functions : f.name => f }

  application_id     = oci_functions_application.this.id
  display_name       = each.value.name
  image              = each.value.image
  memory_in_mbs      = each.value.memory_mbs
  timeout_in_seconds = each.value.timeout_sec
  config             = merge(var.injected_env, each.value.env)
  defined_tags       = var.defined_tags
  freeform_tags      = var.freeform_tags
}

output "application_id" {
  value = oci_functions_application.this.id
}

output "invoke_endpoint" {
  value = oci_functions_application.this.id
}

output "functions" {
  value = [
    for k, f in oci_functions_function.this : {
      name            = k
      id              = f.id
      invoke_endpoint = f.invoke_endpoint
    }
  ]
}
