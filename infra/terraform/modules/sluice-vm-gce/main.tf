resource "google_compute_instance" "worker" {
  name         = var.name
  project      = var.project != "" ? var.project : null
  zone         = var.zone
  machine_type = var.machine_type

  dynamic "guest_accelerator" {
    for_each = var.accelerator_type != "" ? [1] : []
    content {
      type  = var.accelerator_type
      count = var.accelerator_count
    }
  }

  scheduling {
    provisioning_model  = var.spot ? "SPOT" : "STANDARD"
    preemptible         = var.spot
    automatic_restart   = false
    on_host_maintenance = "TERMINATE"
    # Spot preemption DELETES the instance (stateless lifecycle, ADR-012) so its disk is freed and no
    # STOPPED-but-billing instance lingers. (STOP — the old value — left the boot disk billing forever.)
    instance_termination_action = var.spot ? "DELETE" : null
  }

  boot_disk {
    # Explicit: the boot disk is deleted with the instance (the default, pinned so a STOPPED/deleted
    # instance never orphans its disk). auto_delete fires on instance DELETE, never on STOP.
    auto_delete = true
    initialize_params {
      image = var.boot_image
      size  = var.disk_gb
    }
  }

  network_interface {
    network = var.network
    access_config {}
  }

  dynamic "service_account" {
    for_each = var.service_account_email != "" ? [1] : []
    content {
      email  = var.service_account_email
      scopes = ["cloud-platform"]
    }
  }

  labels = {
    sluice-app     = var.app
    sluice-managed = "true"
    sluice-pricing = var.spot ? "spot" : "on-demand"
  }

  metadata = {
    startup-script = templatefile("${path.module}/startup.sh.tpl", {
      app            = var.app
      worker_image   = var.worker_image
      workers_per_vm = var.workers_per_vm
      linger_seconds = var.linger_seconds
      env            = var.env
    })
  }
}
