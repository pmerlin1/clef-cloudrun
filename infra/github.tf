# Numeric claims prevent a deleted/renamed GitHub repository or owner name
# from being re-registered by another identity and gaining deployment access.
resource "google_iam_workload_identity_pool" "github" {
  project                   = var.project_id
  workload_identity_pool_id = "${var.service_name}-github"
  display_name              = "Clef GitHub Actions"
  depends_on                = [google_project_service.required]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "Main branch deployment"
  attribute_mapping = {
    "google.subject"                = "assertion.sub"
    "attribute.repository_id"       = "assertion.repository_id"
    "attribute.repository_owner_id" = "assertion.repository_owner_id"
    "attribute.ref"                 = "assertion.ref"
    "attribute.workflow_ref"        = "assertion.workflow_ref"
  }
  attribute_condition = join(" && ", [
    "assertion.repository_id == '${var.github_repository_id}'",
    "assertion.repository_owner_id == '${var.github_owner_id}'",
    "assertion.ref == 'refs/heads/main'",
    "assertion.workflow_ref == '${var.github_repository}/.github/workflows/deploy.yaml@refs/heads/main'",
  ])
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_impersonate" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository_id/${var.github_repository_id}"
}

resource "google_service_account_iam_member" "runtime_act_as" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = local.deployer_member
}

resource "google_artifact_registry_repository_iam_member" "push" {
  project    = var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.runtime.name
  role       = "roles/artifactregistry.writer"
  member     = local.deployer_member
}

# The uploader can create new objects and inspect their metadata. It cannot
# replace or delete an existing pinned model file, change IAM, or create buckets.
resource "google_project_iam_custom_role" "weights_publisher" {
  project     = var.project_id
  role_id     = "${replace(var.service_name, "-", "_")}_weights_publisher"
  title       = "Clef pinned weights publisher"
  description = "Create and verify pinned model objects without deleting or replacing existing objects."
  permissions = [
    "storage.buckets.get",
    "storage.objects.create",
    "storage.objects.get",
    "storage.objects.list",
  ]
  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "publish" {
  bucket = google_storage_bucket.weights.name
  role   = google_project_iam_custom_role.weights_publisher.name
  member = local.deployer_member
}

resource "google_project_iam_member" "api_consumer" {
  project = var.project_id
  role    = "roles/serviceusage.serviceUsageConsumer"
  member  = local.deployer_member
}
