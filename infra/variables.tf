variable "project_id" {
  description = "Existing Google Cloud project with billing enabled."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,28}[a-z0-9]$", var.project_id))
    error_message = "Use a valid Google Cloud project ID, not its display name or number."
  }
}

variable "region" {
  description = "Cloud Run GPU region; confirm availability and quota for the selected model's GPU before deploying."
  type        = string
  default     = "us-central1"
  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+$", var.region))
    error_message = "Use a Google Cloud region such as us-central1."
  }
}

variable "model_variant" {
  description = "Pinned Clef model variant whose GPU shape is rendered by the deployment workflow."
  type        = string
  default     = "clef"
  validation {
    condition     = contains(["clef", "clef-flash"], var.model_variant)
    error_message = "Use clef or clef-flash."
  }
}

variable "gpu_profile" {
  description = "Deployment GPU shape: model-default preserves the model registry; rtx-pro-6000 selects one RTX PRO 6000, 20 vCPU, 80 GiB, without zonal redundancy."
  type        = string
  default     = "model-default"
  validation {
    condition     = contains(["model-default", "rtx-pro-6000"], var.gpu_profile)
    error_message = "Use model-default or rtx-pro-6000."
  }
}

variable "service_name" {
  description = "Resource prefix; short enough for the runtime and deployer service account IDs."
  type        = string
  default     = "clef"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,19}[a-z0-9]$", var.service_name))
    error_message = "Use 2-21 lowercase letters, digits, or hyphens; start with a letter and end with a letter or digit."
  }
}

variable "github_repository" {
  description = "GitHub OWNER/REPO containing this starter at the repository root."
  type        = string
  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "Use OWNER/REPO."
  }
}

variable "github_repository_id" {
  description = "Immutable numeric repository ID: gh api repos/OWNER/REPO --jq .id."
  type        = string
  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "Use the numeric GitHub repository ID."
  }
}

variable "github_owner_id" {
  description = "Immutable numeric owner ID: gh api repos/OWNER/REPO --jq .owner.id."
  type        = string
  validation {
    condition     = can(regex("^[0-9]+$", var.github_owner_id))
    error_message = "Use the numeric GitHub repository owner ID."
  }
}

variable "invoker_members" {
  description = "Explicit IAM members allowed to call Clef, e.g. user:you@example.com. No public principals."
  type        = set(string)
  validation {
    condition = length(var.invoker_members) > 0 && alltrue([
      for member in var.invoker_members : can(regex("^(user|group|serviceAccount):[^[:space:]]+@[^[:space:]]+$", member))
    ])
    error_message = "Provide at least one explicit user:, group:, or serviceAccount: email. Public and domain-wide principals are not accepted."
  }
}

variable "subnet_cidr" {
  description = "Dedicated Direct VPC subnet; /26 provides headroom for Cloud Run address retention across revisions."
  type        = string
  default     = "10.88.0.0/26"
  validation {
    condition     = can(cidrhost(var.subnet_cidr, 1)) && can(regex("^[0-9.]+/26$", var.subnet_cidr))
    error_message = "Use an IPv4 /26 subnet."
  }
}

variable "labels" {
  description = "Resource labels conforming to CCSF standards."
  type        = map(string)
  default = {
    department          = "gsa-technology"
    department_code     = "tis"
    division            = "tis-infrastructure-and-operations"
    business_owner      = "paul-merlin-at-sfgov-org"
    cloud_type          = "commercial"
    environment         = "dev"
    data_classification = "internal"
    internet_exposure   = "internal-only"
    asset_criticality   = "tier-2"
  }
}
