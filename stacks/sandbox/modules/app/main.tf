terraform {
  required_providers {
    oci = { source = "oracle/oci" }
  }
}

variable "compartment_id" { type = string }
variable "name" { type = string }
variable "vcn_id" { type = string }
variable "subnet_id" { type = string }
variable "public" { type = bool }
variable "allowed_cidr" { type = string }
variable "shape" { type = string }
variable "ocpus" { type = number }
variable "memory_gb" { type = number }
variable "defined_tags" { type = map(string) }
variable "freeform_tags" { type = map(string) }

variable "containers" {
  type = list(object({
    name    = string
    image   = string
    port    = optional(number)
    env     = optional(map(string), {})
    command = optional(list(string))
    args    = optional(list(string))
  }))
}

locals {
  ports = toset([for c in var.containers : tostring(c.port) if c.port != null])
}

data "oci_identity_availability_domains" "ads" {
  compartment_id = var.compartment_id
}

# --- network security group: only the declared ports, only from allowed_cidr

resource "oci_core_network_security_group" "app" {
  compartment_id = var.compartment_id
  vcn_id         = var.vcn_id
  display_name   = "${var.name}-app"
  defined_tags   = var.defined_tags
  freeform_tags  = var.freeform_tags
}

resource "oci_core_network_security_group_security_rule" "ingress" {
  for_each = local.ports

  network_security_group_id = oci_core_network_security_group.app.id
  direction                 = "INGRESS"
  protocol                  = "6"
  source                    = var.allowed_cidr
  source_type               = "CIDR_BLOCK"
  description               = "App port ${each.key}"

  tcp_options {
    destination_port_range {
      min = tonumber(each.key)
      max = tonumber(each.key)
    }
  }
}

resource "oci_core_network_security_group_security_rule" "egress" {
  network_security_group_id = oci_core_network_security_group.app.id
  direction                 = "EGRESS"
  protocol                  = "all"
  destination               = "0.0.0.0/0"
  destination_type          = "CIDR_BLOCK"
}

# --- the container instance -------------------------------------------------

resource "oci_container_instances_container_instance" "this" {
  compartment_id           = var.compartment_id
  availability_domain      = data.oci_identity_availability_domains.ads.availability_domains[0].name
  display_name             = "${var.name}-app"
  shape                    = var.shape
  container_restart_policy = "ALWAYS"
  defined_tags             = var.defined_tags
  freeform_tags            = var.freeform_tags

  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_gb
  }

  vnics {
    subnet_id             = var.subnet_id
    display_name          = "${var.name}-app"
    is_public_ip_assigned = var.public
    nsg_ids               = [oci_core_network_security_group.app.id]
  }

  dynamic "containers" {
    for_each = var.containers
    content {
      display_name          = containers.value.name
      image_url             = containers.value.image
      environment_variables = containers.value.env
      command               = containers.value.command
      arguments             = containers.value.args
    }
  }
}

data "oci_core_vnic" "app" {
  vnic_id = oci_container_instances_container_instance.this.vnics[0].vnic_id
}

output "id" {
  value = oci_container_instances_container_instance.this.id
}

output "public_ip" {
  value = data.oci_core_vnic.app.public_ip_address
}

output "private_ip" {
  value = data.oci_core_vnic.app.private_ip_address
}

output "urls" {
  value = [
    for p in local.ports :
    "http://${coalesce(data.oci_core_vnic.app.public_ip_address, data.oci_core_vnic.app.private_ip_address)}:${p}"
  ]
}
