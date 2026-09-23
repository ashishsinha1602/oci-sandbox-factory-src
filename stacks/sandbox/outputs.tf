output "sandbox" {
  value = {
    id             = var.sandbox_id
    compartment_id = oci_identity_compartment.sandbox.id
    owner          = var.owner
    expires        = local.expires
  }
}

output "adb" {
  value = var.enable_adb ? {
    db_name        = module.adb[0].db_name
    tier           = var.adb_tier
    connect_string = module.adb[0].connect_string
    sql_web_url    = module.adb[0].sql_web_url
    apex_url       = module.adb[0].apex_url
  } : null
}

output "adb_admin_password" {
  value     = var.enable_adb ? module.adb[0].admin_password : null
  sensitive = true
}

output "kafka" {
  value = var.enable_kafka ? {
    mode              = var.kafka_mode
    bootstrap_servers = module.kafka[0].bootstrap_servers
    topics            = var.kafka_topics
    auth_note         = module.kafka[0].auth_note
  } : null
}

output "app" {
  value = var.enable_app ? {
    public_ip  = module.app[0].public_ip
    private_ip = module.app[0].private_ip
    urls       = module.app[0].urls
    containers = [for c in var.app_containers : c.name]
  } : null
}
