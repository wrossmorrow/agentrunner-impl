data "google_project" "current" {
  project_id = var.project_id
}

resource "google_service_account" "gke_nodes" {
  account_id   = "${var.cluster_name}-gke-nodes"
  display_name = "${var.cluster_name} GKE Autopilot Node Service Account"

  depends_on = [google_project_service.apis]
}

locals {
  gke_node_roles = toset([
    "roles/container.defaultNodeServiceAccount",
  ])
}

resource "google_project_iam_member" "gke_nodes" {
  for_each = local.gke_node_roles

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.gke_nodes.email}"
}

resource "google_service_account" "agent_router" {
  account_id   = "${var.cluster_name}-agent-router"
  display_name = "${var.cluster_name} Agent Router Vertex AI Service Account"

  depends_on = [google_project_service.apis]
}

resource "google_project_iam_member" "agent_router_vertex_ai" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.agent_router.email}"

  depends_on = [google_project_service.apis]
}

resource "google_service_account_iam_member" "agent_router_workload_identity" {
  service_account_id = google_service_account.agent_router.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[ai-gateway/agent-router-proxy]"

  depends_on = [google_container_cluster.autopilot]
}

resource "google_billing_budget" "monthly" {
  count = var.billing_account_id != "" ? 1 : 0

  billing_account = var.billing_account_id
  display_name    = "${var.cluster_name} Monthly Budget"

  budget_filter {
    projects = ["projects/${data.google_project.current.number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.monthly_budget_usd)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }

  threshold_rules {
    threshold_percent = 0.8
  }

  threshold_rules {
    threshold_percent = 1.0
  }

  depends_on = [google_project_service.apis]
}
