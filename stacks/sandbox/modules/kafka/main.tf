terraform {
  required_providers {
    oci = { source = "oracle/oci" }
  }
}

variable "compartment_id" { type = string }
variable "name" { type = string }
variable "mode" { type = string }
variable "topics" { type = list(string) }
variable "partitions" { type = number }
variable "kafka_version" { type = string }
variable "subnet_id" { type = string }
variable "defined_tags" { type = map(string) }
variable "freeform_tags" { type = map(string) }

locals {
  streaming = var.mode == "streaming"
}

# --- mode = streaming: OCI Streaming pool with the Kafka-compatible API ------
# Serverless; billed per GB in/out and per partition-hour. Clients use SASL
# PLAIN with <tenancy>/<user>/<pool-ocid> as username and an auth token.

resource "oci_streaming_stream_pool" "this" {
  count          = local.streaming ? 1 : 0
  compartment_id = var.compartment_id
  name           = "${var.name}-kafka"
  defined_tags   = var.defined_tags
  freeform_tags  = var.freeform_tags

  kafka_settings {
    auto_create_topics_enable = true
    log_retention_hours       = 24
    num_partitions            = var.partitions
  }
}

resource "oci_streaming_stream" "topics" {
  for_each = local.streaming ? toset(var.topics) : toset([])

  name               = each.key
  stream_pool_id     = oci_streaming_stream_pool.this[0].id
  partitions         = var.partitions
  retention_in_hours = 24
  defined_tags       = var.defined_tags
  freeform_tags      = var.freeform_tags
}

# --- mode = cluster: managed Kafka, DEVELOPMENT tier -----------------------
# Real brokers in the private subnet. Costs per broker-hour; use for demos
# that need consumer groups, transactions or Kafka Connect.

resource "oci_managed_kafka_kafka_cluster_config" "this" {
  count          = local.streaming ? 0 : 1
  compartment_id = var.compartment_id
  display_name   = "${var.name}-kafka-config"
  defined_tags   = var.defined_tags
  freeform_tags  = var.freeform_tags

  latest_config {
    properties = {
      "auto.create.topics.enable" = "true"
      "num.partitions"            = tostring(var.partitions)
      "log.retention.hours"       = "24"
    }
  }
}

resource "oci_managed_kafka_kafka_cluster" "this" {
  count             = local.streaming ? 0 : 1
  compartment_id    = var.compartment_id
  display_name      = "${var.name}-kafka"
  cluster_type      = "DEVELOPMENT"
  coordination_type = "KRAFT"
  kafka_version     = var.kafka_version

  cluster_config_id      = oci_managed_kafka_kafka_cluster_config.this[0].id
  cluster_config_version = oci_managed_kafka_kafka_cluster_config.this[0].latest_config[0].version_number

  access_subnets {
    subnets = [var.subnet_id]
  }

  broker_shape {
    node_count          = 1
    ocpu_count          = 1
    storage_size_in_gbs = 50
  }

  defined_tags  = var.defined_tags
  freeform_tags = var.freeform_tags
}

output "bootstrap_servers" {
  value = local.streaming ? try(oci_streaming_stream_pool.this[0].kafka_settings[0].bootstrap_servers, "") : try(oci_managed_kafka_kafka_cluster.this[0].kafka_bootstrap_urls[0].url, "")
}

output "pool_id" {
  value = local.streaming ? oci_streaming_stream_pool.this[0].id : null
}

output "auth_note" {
  value = local.streaming ? "SASL_SSL/PLAIN. username = <tenancy-name>/<user-name>/${oci_streaming_stream_pool.this[0].id}, password = an OCI auth token." : "mTLS with the cluster client certificate bundle, or SASL superuser (see cluster superusers)."
}
