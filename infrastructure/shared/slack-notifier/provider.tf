terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket  = "goodparty-terraform-state-us-west-2"
    key     = "shared/slack-notifier/terraform.tfstate"
    region  = "us-west-2"
  }
}

provider "aws" {
  region = "us-west-2"

  default_tags {
    tags = {
      Project     = "GoodParty"
      ManagedBy   = "Terraform"
      Environment = "shared"
    }
  }
}
