output "cluster_name" {
  description = "GKE Autopilot cluster name."
  value       = google_container_cluster.autopilot.name
}

output "region" {
  description = "GCP region where the cluster is deployed."
  value       = var.region
}

output "cluster_endpoint" {
  description = "GKE Autopilot control-plane endpoint."
  value       = google_container_cluster.autopilot.endpoint
  sensitive   = true
}

output "workload_identity_pool" {
  description = "GKE Workload Identity pool for the project."
  value       = "${var.project_id}.svc.id.goog"
}

output "gke_node_service_account" {
  description = "Least-privilege service account email used by GKE Autopilot nodes."
  value       = google_service_account.gke_nodes.email
}

output "vpc_name" {
  description = "Dedicated VPC network name."
  value       = google_compute_network.vpc.name
}

output "get_credentials_command" {
  description = "Command to configure kubectl for the GKE Autopilot cluster."
  value       = "gcloud container clusters get-credentials ${google_container_cluster.autopilot.name} --region ${var.region} --project ${var.project_id}"
}

output "sessions_bucket" {
  description = "GCS bucket for archived interactive and ARC runner sessions."
  value       = google_storage_bucket.sessions.name
}

output "session_hub_service_account" {
  description = "GCP service account email bound to session-system/session-hub via Workload Identity."
  value       = google_service_account.session_hub.email
}
