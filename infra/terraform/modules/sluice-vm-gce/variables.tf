variable "name" {
  type = string
}

variable "app" {
  type = string
}

variable "project" {
  type    = string
  default = ""
}

variable "zone" {
  type    = string
  default = "us-central1-a"
}

variable "machine_type" {
  type    = string
  default = "g2-standard-8"
}

variable "accelerator_type" {
  type    = string
  default = ""
}

variable "accelerator_count" {
  type    = number
  default = 1
}

variable "spot" {
  type    = bool
  default = true
}

variable "boot_image" {
  type    = string
  default = "projects/cos-cloud/global/images/family/cos-stable"
}

variable "disk_gb" {
  type    = number
  default = 100
}

variable "network" {
  type    = string
  default = "default"
}

variable "service_account_email" {
  type    = string
  default = ""
}

variable "worker_image" {
  type    = string
  default = ""
}

variable "workers_per_vm" {
  type    = number
  default = 1
}

variable "linger_seconds" {
  type    = number
  default = 300
}

variable "env" {
  type    = map(string)
  default = {}
}
