terraform {
  required_version = ">= 1.6.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  required_apis = toset([
    "aiplatform.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "dlp.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "sqladmin.googleapis.com",
    "artifactregistry.googleapis.com",
  ])

  runtime_roles = toset([
    "roles/aiplatform.user",
    "roles/secretmanager.secretAccessor",
    "roles/dlp.user",
    "roles/cloudtrace.agent",
    "roles/logging.logWriter",
    "roles/cloudsql.client",
  ])
}

resource "google_project_service" "enabled_apis" {
  for_each           = local.required_apis
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# Least-privilege Service Account for the ADK Expense Agent runtime
resource "google_service_account" "agent_runtime" {
  account_id   = "${var.service_name}-sa"
  display_name = "Enterprise Expense Approval ADK Agent Runtime SA"
  depends_on   = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "agent_runtime_roles" {
  for_each = local.runtime_roles
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.agent_runtime.email}"
}

# Secret Manager containers (secret versions populated out-of-band; never hardcoded)
resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = var.gemini_api_key_secret_id
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret" "session_db_url" {
  secret_id = var.session_db_url_secret_id
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled_apis]
}

# Persistent Cloud SQL PostgreSQL instance backing ADK DatabaseSessionService
resource "google_sql_database_instance" "agent_sessions" {
  name                = "${var.service_name}-sessions-db"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = var.db_deletion_protection

  settings {
    tier = var.db_tier

    backup_configuration {
      enabled = true
    }

    ip_configuration {
      ipv4_enabled = true
    }
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_sql_database" "expense_sessions" {
  name     = "expense_sessions"
  instance = google_sql_database_instance.agent_sessions.name
}

# Cloud DLP De-identification template for active PII redaction
resource "google_data_loss_prevention_deidentify_template" "pii_scrubber" {
  parent       = "projects/${var.project_id}/locations/global"
  display_name = "Expense Agent PII Redaction Template"
  description  = "Redacts emails, SSNs, credit cards, and phone numbers prior to logging or memory storage."

  deidentify_config {
    info_type_transformations {
      transformations {
        primitive_transformation {
          replace_with_info_type_config {}
        }
      }
    }
  }

  depends_on = [google_project_service.enabled_apis]
}

# Cloud Run v2 deployment for the containerized ADK API server
resource "google_cloud_run_v2_service" "expense_agent_service" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  template {
    service_account = google_service_account.agent_runtime.email

    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    containers {
      image = var.container_image

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_LOCATION"
        value = var.region
      }
      env {
        name  = "ENABLE_CLOUD_DLP"
        value = "true"
      }
      env {
        name  = "ENABLE_CLOUD_TRACE"
        value = "true"
      }
      env {
        name  = "ALLOW_LOCAL_ENV_FALLBACK"
        value = "false"
      }
      env {
        name = "GOOGLE_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_key.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SESSION_DB_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.session_db_url.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_iam_member.agent_runtime_roles,
    google_secret_manager_secret.gemini_api_key,
    google_secret_manager_secret.session_db_url,
  ]
}
