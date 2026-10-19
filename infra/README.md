# infra

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.5.0, < 2.0.0 |
| <a name="requirement_google"></a> [google](#requirement\_google) | 7.46.0 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_google"></a> [google](#provider\_google) | 7.46.0 |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [google_artifact_registry_repository.runtime](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/artifact_registry_repository) | resource |
| [google_artifact_registry_repository_iam_member.push](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/artifact_registry_repository_iam_member) | resource |
| [google_cloud_run_v2_service.runtime](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/cloud_run_v2_service) | resource |
| [google_cloud_run_v2_service_iam_policy.runtime](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/cloud_run_v2_service_iam_policy) | resource |
| [google_compute_network.runtime](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/compute_network) | resource |
| [google_compute_subnetwork.runtime](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/compute_subnetwork) | resource |
| [google_iam_workload_identity_pool.github](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/iam_workload_identity_pool) | resource |
| [google_iam_workload_identity_pool_provider.github](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/iam_workload_identity_pool_provider) | resource |
| [google_project_iam_custom_role.weights_publisher](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/project_iam_custom_role) | resource |
| [google_project_iam_member.api_consumer](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/project_iam_member) | resource |
| [google_project_service.required](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/project_service) | resource |
| [google_service_account.deployer](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/service_account) | resource |
| [google_service_account.runtime](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/service_account) | resource |
| [google_service_account_iam_member.github_impersonate](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/service_account_iam_member) | resource |
| [google_service_account_iam_member.runtime_act_as](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/service_account_iam_member) | resource |
| [google_storage_bucket.weights](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/storage_bucket) | resource |
| [google_storage_bucket_iam_member.publish](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/storage_bucket_iam_member) | resource |
| [google_storage_bucket_iam_member.runtime_read](https://registry.terraform.io/providers/hashicorp/google/7.46.0/docs/resources/storage_bucket_iam_member) | resource |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_github_owner_id"></a> [github\_owner\_id](#input\_github\_owner\_id) | Immutable numeric owner ID: gh api repos/OWNER/REPO --jq .owner.id. | `string` | n/a | yes |
| <a name="input_github_repository"></a> [github\_repository](#input\_github\_repository) | GitHub OWNER/REPO containing this starter at the repository root. | `string` | n/a | yes |
| <a name="input_github_repository_id"></a> [github\_repository\_id](#input\_github\_repository\_id) | Immutable numeric repository ID: gh api repos/OWNER/REPO --jq .id. | `string` | n/a | yes |
| <a name="input_invoker_members"></a> [invoker\_members](#input\_invoker\_members) | Explicit IAM members allowed to call Clef, e.g. user:you@example.com. No public principals. | `set(string)` | n/a | yes |
| <a name="input_model_variant"></a> [model\_variant](#input\_model\_variant) | Pinned Clef model variant whose GPU shape is rendered by the deployment workflow. | `string` | `"clef"` | no |
| <a name="input_project_id"></a> [project\_id](#input\_project\_id) | Existing Google Cloud project with billing enabled. | `string` | n/a | yes |
| <a name="input_region"></a> [region](#input\_region) | Cloud Run GPU region; confirm availability and quota for the selected model's GPU before deploying. | `string` | `"us-central1"` | no |
| <a name="input_service_name"></a> [service\_name](#input\_service\_name) | Resource prefix; short enough for the runtime and deployer service account IDs. | `string` | `"clef"` | no |
| <a name="input_subnet_cidr"></a> [subnet\_cidr](#input\_subnet\_cidr) | Dedicated Direct VPC subnet; /26 provides headroom for Cloud Run address retention across revisions. | `string` | `"10.88.0.0/26"` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_github_variables"></a> [github\_variables](#output\_github\_variables) | Non-secret repository variables consumed by .github/workflows/deploy.yaml. |
| <a name="output_image_repository"></a> [image\_repository](#output\_image\_repository) | n/a |
| <a name="output_service_url"></a> [service\_url](#output\_service\_url) | Private URL. Before the workflow deploys, this serves only the bootstrap container. |
| <a name="output_weights_bucket"></a> [weights\_bucket](#output\_weights\_bucket) | n/a |
<!-- END_TF_DOCS -->
