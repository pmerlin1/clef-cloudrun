output "github_variables" {
  description = "Non-secret repository variables consumed by .github/workflows/deploy.yaml, including CLEF_GPU_PROFILE."
  value = {
    GCP_PROJECT_ID         = var.project_id
    GCP_REGION             = var.region
    CLEF_SERVICE           = var.service_name
    CLEF_MODEL             = var.model_variant
    CLEF_GPU_PROFILE       = var.gpu_profile
    WIF_PROVIDER           = google_iam_workload_identity_pool_provider.github.name
    DEPLOY_SERVICE_ACCOUNT = google_service_account.deployer.email
  }
}

output "service_url" {
  description = "Private URL. Before the workflow deploys, this serves only the bootstrap container."
  value       = google_cloud_run_v2_service.runtime.uri
}

output "weights_bucket" {
  value = google_storage_bucket.weights.name
}

output "image_repository" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${var.service_name}/api"
}
