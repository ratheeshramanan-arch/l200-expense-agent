variable "project_id" {
  description = "Google Cloud project ID where the ADK agent and supporting services are provisioned."
  type        = string
}

variable "region" {
  description = "Google Cloud region for Cloud Run, Cloud SQL, and Vertex AI Agent Engine."
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Resource name prefix for the Enterprise Expense Approval Agent."
  type        = string
  default     = "l200-expense-agent"
}

variable "container_image" {
  description = "Artifact Registry container image URI built from the root Dockerfile."
  type        = string
}

variable "gemini_api_key_secret_id" {
  description = "Secret Manager ID storing the Gemini API credential."
  type        = string
  default     = "gemini-api-key"
}

variable "session_db_url_secret_id" {
  description = "Secret Manager ID storing the Cloud SQL connection URL."
  type        = string
  default     = "session-db-url"
}

variable "db_tier" {
  description = "Cloud SQL PostgreSQL machine tier."
  type        = string
  default     = "db-f1-micro"
}

variable "db_deletion_protection" {
  description = "Enable deletion protection on the Cloud SQL session database."
  type        = bool
  default     = true
}

variable "max_instances" {
  description = "Maximum number of autoscaled Cloud Run instances."
  type        = number
  default     = 5
}
