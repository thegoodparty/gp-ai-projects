# GitHub Actions OIDC Provider for AWS
# This allows GitHub Actions to authenticate to AWS without long-lived credentials

# Get current AWS account ID
data "aws_caller_identity" "current" {}

# Create OIDC provider for GitHub Actions
resource "aws_iam_openid_connect_provider" "github_actions" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = {
    Name        = "GitHub Actions OIDC Provider"
    Purpose     = "Allow GitHub Actions to assume AWS roles"
  }
}

# IAM role for GitHub Actions to push to ECR
resource "aws_iam_role" "github_actions_ecr_push" {
  name = "github-actions-ecr-push"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = aws_iam_openid_connect_provider.github_actions.arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            # Replace with your GitHub org/repo
            "token.actions.githubusercontent.com:sub" = "repo:goodparty/gp-ai-projects:*"
          }
        }
      }
    ]
  })

  tags = {
    Name        = "GitHub Actions ECR Push Role"
    Purpose     = "Allow GitHub Actions to push Docker images to ECR"
  }
}

# Policy for ECR push access
resource "aws_iam_role_policy" "github_actions_ecr_push" {
  name = "ecr-push-policy"
  role = aws_iam_role.github_actions_ecr_push.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:GetRepositoryPolicy",
          "ecr:DescribeRepositories",
          "ecr:ListImages",
          "ecr:DescribeImages",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage"
        ]
        Resource = [
          "arn:aws:ecr:us-west-2:${data.aws_caller_identity.current.account_id}:repository/gp-ai-projects"
        ]
      }
    ]
  })
}

# Output the role ARN for use in GitHub Actions
output "github_actions_role_arn" {
  value       = aws_iam_role.github_actions_ecr_push.arn
  description = "ARN of the IAM role for GitHub Actions to push to ECR"
}
