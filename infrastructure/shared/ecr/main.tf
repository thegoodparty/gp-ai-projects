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
        description  = "Never expire environment images (main, master, prod, qa, dev, release)"
        selection = {
          tagStatus = "tagged"
          tagPrefixList = [
            "main",
            "master",
            "prod",
            "qa",
            "dev",
            "release"
          ]
          countType   = "imageCountMoreThan"
          countNumber = 999
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 2
        description  = "Keep versioned releases (v1.0.0, v2.1.3) for 365 days"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["v1.", "v2.", "v3.", "v4.", "v5."]
          countType     = "sinceImagePushed"
          countUnit     = "days"
          countNumber   = 365
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 3
        description  = "Keep serve-analyze images for 180 days (stable project)"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["serve-analyze-"]
          countType     = "sinceImagePushed"
          countUnit     = "days"
          countNumber   = 180
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 4
        description  = "Keep campaign-planner images for 90 days (active development)"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["campaign-planner-"]
          countType     = "sinceImagePushed"
          countUnit     = "days"
          countNumber   = 90
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 5
        description  = "Keep latest/dev/staging tags for 60 days"
        selection = {
          tagStatus     = "tagged"
          tagPrefixList = ["latest", "dev", "staging"]
          countType     = "sinceImagePushed"
          countUnit     = "days"
          countNumber   = 60
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 6
        description  = "Keep untagged images for 90 days (previous latest builds)"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 90
        }
        action = {
          type = "expire"
        }
      },
      {
        rulePriority = 7
        description  = "Cleanup any other images after 30 days"
        selection = {
          tagStatus   = "any"
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
