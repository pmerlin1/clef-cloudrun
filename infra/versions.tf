terraform {
  required_version = ">= 1.5.0, < 2.0.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "7.46.0"
    }
  }
  # Bootstrap uses local state. Keep it private, backed up, and out of Git.
  # Add a private remote backend before collaborating on this infrastructure.
}

provider "google" {
  project = var.project_id
  region  = var.region
}
