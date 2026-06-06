variable "name" {
  type = string
}

variable "app" {
  type = string
}

variable "ami" {
  type    = string
  default = ""
}

variable "instance_type" {
  type    = string
  default = "g6.xlarge"
}

variable "spot" {
  type    = bool
  default = true
}

variable "subnet_id" {
  type    = string
  default = ""
}

variable "iam_instance_profile" {
  type    = string
  default = ""
}

variable "disk_gb" {
  type    = number
  default = 100
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
