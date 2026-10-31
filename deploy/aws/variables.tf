variable "name" {
  description = "Unique resource prefix in this account and region."
  type        = string
  default     = "moyai"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.name))
    error_message = "Use 3–24 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "hostname" {
  description = "Dedicated HTTPS hostname inside the supplied public Route 53 zone."
  type        = string
  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]+\\.[a-z]{2,}$", var.hostname))
    error_message = "Supply a lowercase DNS hostname, without scheme or path."
  }
}

variable "route53_zone_id" {
  description = "Existing public hosted zone. This stack creates only its own application and certificate records."
  type        = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.72.0.0/16"
  validation {
    condition     = can(cidrsubnet(var.vpc_cidr, 8, 3))
    error_message = "Supply an IPv4 VPC CIDR with room for four subnets."
  }
}

variable "instance_type" {
  type    = string
  default = "t3.xlarge"
}

variable "database_instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "deletion_protection" {
  description = "Protect RDS and the ALB against accidental teardown. Disabling does not empty S3 or backup vaults."
  type        = bool
  default     = true
}

variable "moyai_image" {
  description = "Immutable, anonymously pullable application image matching this checkout's Docker bundle."
  type        = string
  default     = "ghcr.io/berriai/moyai@sha256:6ea304813079463b31a065fa8886612929bb9bfce1c5b0e68c7b7d129648a34a"
  validation {
    condition     = can(regex("^[a-z0-9./:_-]+@sha256:[0-9a-f]{64}$", var.moyai_image))
    error_message = "Pin the application image by sha256 digest."
  }
}

variable "litellm_image" {
  description = "Immutable LiteLLM proxy image; the default was qualified with this quickstart."
  type        = string
  default     = "ghcr.io/berriai/litellm@sha256:37e2dafb088a7723beb998590682957386dfc83ffea1848486f8e0e5cfac0b06"
  validation {
    condition     = can(regex("^[a-z0-9./:_-]+@sha256:[0-9a-f]{64}$", var.litellm_image))
    error_message = "Pin the gateway image by sha256 digest."
  }
}

variable "bedrock_model_id" {
  description = "Enabled Bedrock model/inference profile, without the bedrock/ provider prefix."
  type        = string
  default     = "us.anthropic.claude-sonnet-4-6"
}

variable "bedrock_invoke_resources" {
  description = "Exact model and inference-profile ARNs allowed for inference, including destination-region models for cross-region profiles."
  type        = list(string)
  validation {
    condition     = length(var.bedrock_invoke_resources) > 0 && alltrue([for arn in var.bedrock_invoke_resources : can(regex("^arn:aws:bedrock:[a-z0-9-]+:[0-9]*:(foundation-model|inference-profile|application-inference-profile)/[^*]+$", arn))])
    error_message = "Supply explicit Bedrock model/profile ARNs without wildcards."
  }
}

variable "computer_lifetime" {
  type    = number
  default = 28800
  validation {
    condition     = var.computer_lifetime >= 300 && var.computer_lifetime <= 28800 && floor(var.computer_lifetime) == var.computer_lifetime
    error_message = "Use an integer lifetime between 300 and 28800 seconds."
  }
}

variable "computer_idle_timeout" {
  type    = number
  default = 600
  validation {
    condition     = var.computer_idle_timeout >= 60 && var.computer_idle_timeout <= 28800 && floor(var.computer_idle_timeout) == var.computer_idle_timeout
    error_message = "Use an integer idle timeout between 60 and 28800 seconds."
  }
}
