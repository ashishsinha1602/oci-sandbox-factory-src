terraform {
  required_version = ">= 1.5"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 6.0"
    }
    time = {
      source  = "hashicorp/time"
      version = ">= 0.9"
    }
  }
}

# No auth here. Locally: ~/.oci/config. In Resource Manager: automatic.
provider "oci" {
  tenancy_ocid = var.tenancy_ocid
  region       = var.region
}
