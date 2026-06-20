resource "aws_instance" "worker" {
  ami                  = var.ami
  instance_type        = var.instance_type
  subnet_id            = var.subnet_id != "" ? var.subnet_id : null
  iam_instance_profile = var.iam_instance_profile != "" ? var.iam_instance_profile : null

  # A guest `shutdown -h now` (the agent's idle self-terminate) TERMINATES the instance — mirrors GCE's
  # self-delete so no STOPPED-but-billing instance lingers (stateless lifecycle, ADR-012).
  instance_initiated_shutdown_behavior = "terminate"

  dynamic "instance_market_options" {
    for_each = var.spot ? [1] : []
    content {
      market_type = "spot"
      spot_options {
        # Spot interruption TERMINATES (not stop) → instance + root volume freed. `terminate` requires a
        # one-time request (AWS rejects persistent + terminate).
        instance_interruption_behavior = "terminate"
        spot_instance_type             = "one-time"
      }
    }
  }

  root_block_device {
    volume_size           = var.disk_gb
    delete_on_termination = true # explicit: root volume dies with the instance (no orphaned EBS)
  }

  user_data = templatefile("${path.module}/startup.sh.tpl", {
    app            = var.app
    worker_image   = var.worker_image
    workers_per_vm = var.workers_per_vm
    linger_seconds = var.linger_seconds
    env            = var.env
  })

  tags = {
    Name             = var.name
    "sluice-app"     = var.app
    "sluice-managed" = "true"
    "sluice-pricing" = var.spot ? "spot" : "on-demand"
  }
}
