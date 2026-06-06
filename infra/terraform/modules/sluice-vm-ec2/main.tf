resource "aws_instance" "worker" {
  ami                  = var.ami
  instance_type        = var.instance_type
  subnet_id            = var.subnet_id != "" ? var.subnet_id : null
  iam_instance_profile = var.iam_instance_profile != "" ? var.iam_instance_profile : null

  dynamic "instance_market_options" {
    for_each = var.spot ? [1] : []
    content {
      market_type = "spot"
      spot_options {
        instance_interruption_behavior = "stop"
        spot_instance_type             = "persistent"
      }
    }
  }

  root_block_device {
    volume_size = var.disk_gb
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
