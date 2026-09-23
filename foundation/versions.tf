terraform {
  required_version = ">= 1.5"
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 6.0"
    }
  }
}

# Auth is intentionally NOT configured here.
#   Local:            ~/.oci/config [DEFAULT] profile (or OCI_CLI_PROFILE env)
#   Resource Manager: automatic (resource principal)
# Same code runs in both places with no changes.
provider "oci" {
  tenancy_ocid = var.tenancy_ocid
  region       = var.region
}
