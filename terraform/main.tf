provider "google" {
  project               = var.project_id
  region                = var.region
  billing_project       = var.project_id
  user_project_override = true
  default_labels        = var.labels
}

locals {
  base_services = [
    "container.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "aiplatform.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
  ]
  enabled_services = toset(concat(
    local.base_services,
    var.billing_account_id != "" ? ["billingbudgets.googleapis.com"] : []
  ))
  pods_range_name     = "${var.cluster_name}-pods"
  services_range_name = "${var.cluster_name}-services"
}

resource "google_project_service" "apis" {
  for_each = local.enabled_services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "vpc" {
  name                    = "${var.cluster_name}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"

  depends_on = [google_project_service.apis]
}

resource "google_compute_subnetwork" "gke" {
  name                     = "${var.cluster_name}-subnet"
  ip_cidr_range            = var.subnet_cidr
  region                   = var.region
  network                  = google_compute_network.vpc.id
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = local.pods_range_name
    ip_cidr_range = var.pods_cidr
  }

  secondary_ip_range {
    range_name    = local.services_range_name
    ip_cidr_range = var.services_cidr
  }
}

resource "google_compute_router" "nat" {
  name    = "${var.cluster_name}-router"
  region  = var.region
  network = google_compute_network.vpc.id
}

resource "google_compute_router_nat" "nat" {
  name                                = "${var.cluster_name}-nat"
  router                              = google_compute_router.nat.name
  region                              = var.region
  nat_ip_allocate_option              = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat  = "LIST_OF_SUBNETWORKS"
  enable_endpoint_independent_mapping = false

  subnetwork {
    name                    = google_compute_subnetwork.gke.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

resource "google_container_cluster" "autopilot" {
  name                = var.cluster_name
  location            = var.region
  enable_autopilot    = true
  deletion_protection = var.deletion_protection

  network    = google_compute_network.vpc.id
  subnetwork = google_compute_subnetwork.gke.id

  release_channel {
    channel = var.release_channel
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = local.pods_range_name
    services_secondary_range_name = local.services_range_name
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
    master_ipv4_cidr_block  = var.master_ipv4_cidr_block
  }

  cluster_autoscaling {
    auto_provisioning_defaults {
      service_account = google_service_account.gke_nodes.email
    }
  }

  gateway_api_config {
    channel = "CHANNEL_STANDARD"
  }

  dynamic "master_authorized_networks_config" {
    for_each = length(var.authorized_networks) > 0 ? [1] : []
    content {
      dynamic "cidr_blocks" {
        for_each = var.authorized_networks
        content {
          cidr_block   = cidr_blocks.value.cidr_block
          display_name = cidr_blocks.value.display_name
        }
      }
    }
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }

  depends_on = [
    google_project_service.apis,
    google_compute_router_nat.nat,
    google_project_iam_member.gke_nodes,
  ]
}
