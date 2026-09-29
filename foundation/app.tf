# What the installer needs to start using the factory: the application URL and
# a first login. The workers create the application on their first start
# (factory/bootstrap.py) with this admin password, so it works straight away.

resource "random_password" "app_admin" {
  length           = 20
  min_upper        = 2
  min_lower        = 2
  min_numeric      = 2
  min_special      = 1
  override_special = "#_-"
}

locals {
  # the installer's own choice from the form, or a generated one
  app_admin_user     = upper(var.app_admin_user)
  app_admin_password = var.app_admin_password != "" ? var.app_admin_password : random_password.app_admin.result
  app_url            = var.enable_control_adb ? try(replace(oci_database_autonomous_database.control[0].connection_urls[0].apex_url, "/ords/apex", "/ords/r/sbx/sandbox-factory/"), "") : ""
}

output "app_url" {
  description = "The application. It appears at this URL a few minutes after this apply finishes (up to 10 on the Free Tier edition): the worker installs it on its first start. Until then the URL shows 404; that is normal. Sign in with app_admin_user / app_admin_password."
  value       = local.app_url
}

output "next_step" {
  description = "What to do once this apply is green."
  value       = "Wait a few minutes (Free Tier edition: up to 10), then open app_url and sign in with app_admin_user / app_admin_password. A 404 means the worker is still installing the application; status_url answers as soon as the worker has started and says which step it is on."
}

output "status_url" {
  description = "Install progress as JSON, from the worker's first minute on: which bootstrap step is running, and 'ready' with the application URL when done. 404 until the worker's first start."
  value       = var.enable_control_adb ? try(replace(oci_database_autonomous_database.control[0].connection_urls[0].apex_url, "/ords/apex", "/ords/admin/status/"), "") : ""
}

output "app_admin_user" {
  value = local.app_admin_user
}

output "app_admin_password" {
  value     = local.app_admin_password
  sensitive = true
}
