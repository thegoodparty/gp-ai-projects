provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "GoodParty Campaign Platform"
      Environment = var.environment
      ManagedBy   = "Terraform"
    }
  }
}