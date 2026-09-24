# ---------------------------------------------------------------------------
# Set automatically by Resource Manager. Locally they come from a tfvars file.
# ---------------------------------------------------------------------------
variable "tenancy_ocid" {
  type = string
}

variable "region" {
  type = string
}

variable "compartment_ocid" {
  type        = string
  description = "Compartment the stack itself lives in (sbx-control). Unused by resources; kept so Resource Manager can populate it."
  default     = null
}

# ---------------------------------------------------------------------------
# Foundation outputs (terraform output -json in ../../foundation)
# ---------------------------------------------------------------------------
variable "control_compartment_ocid" {
  type        = string
  description = "OCID of sbx-control. The worker runs here and creates its Resource Manager stacks here."
}

variable "sandboxes_compartment_ocid" {
  type        = string
  description = "OCID of sbx-sandboxes. Everything the worker builds lands under it."
}

variable "vcn_id" {
  type = string
}

variable "public_subnet_id" {
  type        = string
  description = "Passed through to the worker as part of SBX_FOUNDATION; the worker itself does not sit here."
}

variable "private_subnet_id" {
  type        = string
  description = "The worker's own subnet. Needs a NAT gateway (OCIR, ADB) and a service gateway."
}

variable "tag_namespace" {
  type        = string
  default     = "sbx"
  description = "Defined-tag namespace created by the foundation stack."
}

# ---------------------------------------------------------------------------
# Control database
# ---------------------------------------------------------------------------
variable "control_connect_string" {
  type        = string
  description = <<-EOT
    host:port/service for SBXCTL, in exactly that shape - controldb._dsn() splits on
    "/" then ":" and builds the descriptor itself. Use the 1522 (TLS) profile, not
    1521: the worker has no wallet, so mTLS must not be required on the database.

    e.g. adb.us-phoenix-1.oraclecloud.com:1522/g63702d3fbc0a2d_sbxctl_low.adb.oraclecloud.com
  EOT

  validation {
    condition     = can(regex("^[^:/]+:[0-9]+/[^:/]+$", var.control_connect_string))
    error_message = "control_connect_string must be host:port/service, e.g. adb.us-phoenix-1.oraclecloud.com:1522/xxx_sbxctl_low.adb.oraclecloud.com"
  }
}

variable "control_admin_password" {
  type        = string
  sensitive   = true
  description = "ADMIN password for SBXCTL. Read by controldb.connect('ADMIN') via SBX_ADMIN_PASSWORD."
}

variable "owner" {
  type        = string
  description = "Email stamped on anything the worker creates that does not name its own requester."
}

# ---------------------------------------------------------------------------
# The worker container
# ---------------------------------------------------------------------------
variable "ocir_user" {
  type        = string
  default     = ""
  description = "OCIR username for pushing built images, e.g. your OCI username or <tenancy>/<user>. Required for Git-URL deploys."
}

variable "ocir_token" {
  type        = string
  default     = ""
  sensitive   = true
  description = <<-EOT
    OCI auth token for that user. oci_build.py mounts it as kaniko's docker
    config to push the image it builds; without it a Git-URL deploy fails with
    "OCIR_TOKEN ... is required to push the built image".

    A user may hold at most two auth tokens, and their values are shown only
    once at creation - so this has to be supplied, not discovered.
  EOT
}

variable "worker_image" {
  type        = string
  description = "Full OCIR path of the worker image, e.g. phx.ocir.io/<namespace>/sbx/factory/worker:latest"
}

variable "worker_shape" {
  type    = string
  default = "CI.Standard.A1.Flex"
}

variable "worker_ocpus" {
  type    = number
  default = 1
}

variable "worker_memory_gb" {
  type    = number
  default = 6
}

variable "create_iam" {
  type        = bool
  default     = true
  description = <<-EOT
    Create the dynamic group and the tenancy-level policy. Both are tenancy-root
    objects, so whoever applies this stack needs to administer the root compartment.
    Set false if IAM is managed separately, then grant the worker the equivalent
    rights by hand before it will do anything.
  EOT
}
