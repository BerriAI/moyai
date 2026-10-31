terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.71" }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Application = "moyai", Installation = var.name, ManagedBy = "Terraform" }
  }
}
