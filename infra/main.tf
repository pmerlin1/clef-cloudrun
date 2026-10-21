locals {
  required_apis = toset([
    "artifactregistry.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
    "sts.googleapis.com",
  ])
  deployer_member = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_project_service" "required" {
  for_each           = local.required_apis
  project            = var.project_id
  service            = each.key
  disable_on_destroy = false
}

resource "google_storage_bucket" "weights" {
  project                     = var.project_id
  name                        = "${var.project_id}-${var.service_name}-weights"
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  labels                      = var.labels
  # Model files are reproducible. Avoid charging for retained deleted copies.
  soft_delete_policy { retention_duration_seconds = 0 }
  depends_on = [google_project_service.required]
}

resource "google_artifact_registry_repository" "runtime" {
  project       = var.project_id
  location      = var.region
  repository_id = var.service_name
  format        = "DOCKER"
  labels        = var.labels
  depends_on    = [google_project_service.required]
}

resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = "${var.service_name}-runtime"
  display_name = "Clef model runtime (read-only weights)"
  depends_on   = [google_project_service.required]
}

resource "google_service_account" "deployer" {
  project      = var.project_id
  account_id   = "${var.service_name}-deployer"
  display_name = "Clef GitHub Actions deployer"
  depends_on   = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "runtime_read" {
  bucket = google_storage_bucket.weights.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

# Direct VPC + Private Google Access: no always-on connector or Cloud NAT.
# The standard Cloud Run service agent role authorizes same-project networking.
resource "google_compute_network" "runtime" {
  project                 = var.project_id
  name                    = var.service_name
  auto_create_subnetworks = false
  depends_on              = [google_project_service.required]
}

resource "google_compute_subnetwork" "runtime" {
  project                  = var.project_id
  name                     = var.service_name
  region                   = var.region
  network                  = google_compute_network.runtime.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true

  log_config {
    aggregation_interval = "INTERVAL_10_MIN"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_cloud_run_v2_service" "runtime" {
  project              = var.project_id
  name                 = var.service_name
  location             = var.region
  deletion_protection  = false
  invoker_iam_disabled = false
  ingress              = "INGRESS_TRAFFIC_ALL"
  labels               = var.labels
  scaling {
    min_instance_count = 0
    max_instance_count = 1
  }

  # Terraform creates only this inexpensive private bootstrap. The manual
  # GitHub workflow owns the complete GPU revision after the first release.
  template {
    service_account = google_service_account.runtime.email
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello:latest"
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
    }
  }
  depends_on = [google_storage_bucket_iam_member.runtime_read]
  lifecycle {
    ignore_changes = [template, client, client_version]
  }
}

# Authoritative at the service level. Project/organization IAM is inherited
# separately; check those policies before using an existing shared project.
resource "google_cloud_run_v2_service_iam_policy" "runtime" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.runtime.name
  policy_data = jsonencode({
    bindings = [
      { role = "roles/run.invoker", members = sort(tolist(var.invoker_members)) },
      { role = "roles/run.developer", members = [local.deployer_member] },
    ]
  })
}
