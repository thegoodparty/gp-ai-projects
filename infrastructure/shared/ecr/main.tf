terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "shared/ecr/terraform.tfstate"
    region = "us-west-2"

    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = "us-west-2"
}

resource "aws_ecr_repository" "ai_projects" {
  name                 = "gp-ai-projects"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name    = "GP AI Projects"
    Project = "ai-projects"
  }
}

resource "aws_ecr_lifecycle_policy" "ai_projects" {
  repository = aws_ecr_repository.ai_projects.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Cleanup untagged images after 30 days (referenced images protected by AWS)"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 30
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

output "repository_url" {
  value       = aws_ecr_repository.ai_projects.repository_url
  description = "ECR repository URL for gp-ai-projects"
}

output "repository_name" {
  value       = aws_ecr_repository.ai_projects.name
  description = "ECR repository name"
}

output "repository_arn" {
  value       = aws_ecr_repository.ai_projects.arn
  description = "ECR repository ARN"
}
