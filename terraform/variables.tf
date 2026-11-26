variable "project_id" {
  description = "GCP project ID for the GKE Autopilot deployment."
  type        = string
}

variable "region" {
  description = "GCP region for the VPC, Cloud NAT, and regional GKE Autopilot cluster."
  type        = string
  default     = "us-central1"
}

variable "cluster_name" {
  description = "Name of the GKE Autopilot cluster and prefix for network/IAM resources."
  type        = string
  default     = "agentreview"
}

variable "release_channel" {
  description = "GKE release channel (RAPID, REGULAR, or STABLE)."
  type        = string
  default     = "REGULAR"

  validation {
    condition     = contains(["RAPID", "REGULAR", "STABLE"], var.release_channel)
    error_message = "release_channel must be one of RAPID, REGULAR, or STABLE."
  }
}

variable "subnet_cidr" {
  description = "Primary IPv4 CIDR range for the GKE Autopilot subnet."
  type        = string
  default     = "10.10.0.0/20"
}

variable "pods_cidr" {
  description = "Secondary IPv4 CIDR range for GKE Autopilot pods."
  type        = string
  default     = "10.20.0.0/16"
}

variable "services_cidr" {
  description = "Secondary IPv4 CIDR range for Kubernetes services."
  type        = string
  default     = "10.30.0.0/20"
}

variable "master_ipv4_cidr_block" {
  description = "Private IPv4 /28 CIDR range for the GKE control plane."
  type        = string
  default     = "172.16.0.0/28"
}

variable "authorized_networks" {
  description = "Optional CIDR blocks authorized to reach the GKE control plane endpoint. Empty leaves IAM-authenticated access open."
  type = list(object({
    cidr_block   = string
    display_name = string
  }))
  default = []
}

variable "deletion_protection" {
  description = "Protect the GKE Autopilot cluster from accidental Terraform deletion."
  type        = bool
  default     = false
}

variable "billing_account_id" {
  description = "Optional GCP Billing Account ID (e.g. 012345-6789AB-CDEF01). When non-empty, creates a monthly budget alert."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly GCP billing budget alert threshold in USD."
  type        = number
  default     = 50
}

variable "labels" {
  description = "Default labels applied to GCP resources."
  type        = map(string)
  default = {
    managed_by = "terraform"
    project    = "agentreview"
  }
}
