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
variable "sandboxes_compartment_ocid" {
  type        = string
  description = "OCID of sbx-sandboxes. The sandbox compartment is created under it."
}

variable "vcn_id" {
  type = string
}

variable "public_subnet_id" {
  type = string
}

variable "private_subnet_id" {
  type = string
}

variable "tag_namespace" {
  type        = string
  default     = "sbx"
  description = "Defined-tag namespace created by the foundation stack."
}

# ---------------------------------------------------------------------------
# Who / what / how long
# ---------------------------------------------------------------------------
variable "sandbox_id" {
  type        = string
  description = "Short unique id, e.g. demo1 or ashish-kafka. Used in every resource name."

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,19}$", var.sandbox_id))
    error_message = "sandbox_id must be 2-20 chars, lowercase letters, digits or dashes, starting with a letter."
  }
}

variable "owner" {
  type        = string
  description = "Requester email or principal name."
}

variable "team" {
  type    = string
  default = "personal"
}

variable "ttl_days" {
  type        = number
  default     = 7
  description = "Days until the reaper destroys this sandbox."

  validation {
    condition     = var.ttl_days >= 1 && var.ttl_days <= 30
    error_message = "ttl_days must be between 1 and 30."
  }
}

variable "allowed_cidr" {
  type        = string
  default     = "0.0.0.0/0"
  description = "Source CIDR allowed to reach public endpoints (app port, free-tier ADB)."
}

# ---------------------------------------------------------------------------
# ADB
# ---------------------------------------------------------------------------
variable "enable_adb" {
  type    = bool
  default = false
}

variable "adb_tier" {
  type        = string
  default     = "free"
  description = "free = Always Free (public endpoint + IP allow-list). paid = ECPU with a private endpoint in the private subnet."

  validation {
    condition     = contains(["free", "paid"], var.adb_tier)
    error_message = "adb_tier must be free or paid."
  }
}

variable "adb_workload" {
  type        = string
  default     = "OLTP"
  description = "OLTP (ATP), DW (ADW), AJD (JSON) or APEX."
}

variable "adb_ecpu_count" {
  type    = number
  default = 2
}

variable "adb_storage_gb" {
  type    = number
  default = 20
}

# ---------------------------------------------------------------------------
# Kafka
# ---------------------------------------------------------------------------
variable "enable_kafka" {
  type    = bool
  default = false
}

variable "kafka_mode" {
  type        = string
  default     = "streaming"
  description = "streaming = OCI Streaming with the Kafka-compatible endpoint (serverless, cheap). cluster = managed Kafka DEVELOPMENT cluster (real brokers, costs per hour)."

  validation {
    condition     = contains(["streaming", "cluster"], var.kafka_mode)
    error_message = "kafka_mode must be streaming or cluster."
  }
}

variable "kafka_topics" {
  type    = list(string)
  default = ["events"]
}

variable "kafka_partitions" {
  type    = number
  default = 1
}

variable "kafka_version" {
  type    = string
  default = "3.7.0"
}

# ---------------------------------------------------------------------------
# App (Container Instance, tier 1)
# ---------------------------------------------------------------------------
variable "enable_app" {
  type    = bool
  default = false
}

variable "app_containers" {
  type = list(object({
    name    = string
    image   = string
    port    = optional(number)
    env     = optional(map(string), {})
    command = optional(list(string))
    args    = optional(list(string))
  }))
  default = [{
    name  = "web"
    image = "docker.io/library/nginx:alpine"
    port  = 80
  }]
  description = "Containers that share one network namespace. Ports listed here are opened to allowed_cidr."
}

variable "app_public" {
  type        = bool
  default     = true
  description = "true = public subnet with a public IP. false = private subnet only."
}

variable "app_shape" {
  type    = string
  default = "CI.Standard.A1.Flex"
}

variable "app_ocpus" {
  type    = number
  default = 1
}

variable "app_memory_gb" {
  type    = number
  default = 4
}
