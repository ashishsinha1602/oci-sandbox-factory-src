output "sandbox" {
  value = {
    id             = var.sandbox_id
    compartment_id = local.compartment_id
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

output "buckets" {
  value = length(module.storage) > 0 ? module.storage[0].buckets : []
}

output "queues" {
  value = length(module.queue) > 0 ? module.queue[0].queues : []
}

output "catalog" {
  value = length(module.catalog) > 0 ? module.catalog[0].catalog : null
}

output "dataflow_jobs" {
  value = length(module.dataflow) > 0 ? module.dataflow[0].jobs : []
}

output "functions" {
  value = length(var.functions) > 0 ? module.functions[0].functions : []
}

output "app_instances" {
  value = [
    for k, m in module.app_extra : {
      name = k
      urls = m.urls
    }
  ]
}

output "databases" {
  description = "Every database in this sandbox: the primary one plus any extras."
  value = concat(
    var.enable_adb ? [{
      name           = "primary"
      db_name        = module.adb[0].db_name
      tier           = var.adb_tier
      connect_string = module.adb[0].connect_string
      sql_web_url    = module.adb[0].sql_web_url
    }] : [],
    [for k, m in module.adb_extra : {
      name           = k
      db_name        = m.db_name
      tier           = try([for d in var.adb_databases : d.tier if d.name == k][0], "free")
      connect_string = m.connect_string
      sql_web_url    = m.sql_web_url
    }]
  )
}

output "nosql" {
  value = var.enable_nosql ? {
    tables         = module.nosql[0].tables
    compartment_id = local.compartment_id
  } : null
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
    url        = coalesce(module.app[0].gateway_url, try(module.app[0].urls[0], null))
    public_ip  = module.app[0].public_ip
    private_ip = module.app[0].private_ip
    urls       = module.app[0].urls
    containers = [for c in var.app_containers : c.name]
  } : null
}
