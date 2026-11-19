output "cloud_run_service_url" {
  description = "Deployed HTTPS URL of the Enterprise Expense Approval ADK service."
  value       = google_cloud_run_v2_service.expense_agent_service.uri
}

output "runtime_service_account_email" {
  description = "Least-privilege runtime service account attached to the agent."
  value       = google_service_account.agent_runtime.email
}

output "cloud_sql_connection_name" {
  description = "Cloud SQL instance connection name for ADK DatabaseSessionService."
  value       = google_sql_database_instance.agent_sessions.connection_name
}

output "dlp_deidentify_template_name" {
  description = "Cloud DLP De-identification template resource name."
  value       = google_data_loss_prevention_deidentify_template.pii_scrubber.name
}
