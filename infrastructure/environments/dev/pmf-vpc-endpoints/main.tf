# ---------------------------------------------------------------------------
# PMF VPC Endpoints — shared infra serving all PMF envs that live in the same
# VPC (vpc-0763fa52c32ebcf6a). Do NOT destroy without coordination — other
# services in the VPC transparently use these endpoints once private_dns is
# enabled.
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.5.0"

  backend "s3" {
    bucket       = "goodparty-terraform-state-us-west-2"
    key          = "pmf-vpc-endpoints/dev/terraform.tfstate"
    region       = "us-west-2"
    use_lockfile = true
    encrypt      = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-west-2"
}

locals {
  vpc_id             = "vpc-0763fa52c32ebcf6a"
  private_subnet_ids = ["subnet-053357b931f0524d4", "subnet-0bb591861f72dcb7f"]
  route_table_ids    = ["rtb-0709e1dbae02f0215", "rtb-0e77d602e8d981b55"]
}

data "aws_vpc" "selected" {
  id = local.vpc_id
}

# ---------------------------------------------------------------------------
# Endpoint Security Group
# ---------------------------------------------------------------------------
# Ingress 443 from the entire VPC CIDR so any service in the VPC (PMF runner,
# PMF broker, gp-api, people-api, election-api, etc.) can reach endpoints
# without cutting anyone off. Runner's egress side is still narrowly scoped
# (agent-sg egress 443 → vpce-sg), so permissive endpoint ingress does NOT
# weaken PMF's quarantine.

resource "aws_security_group" "vpce" {
  name        = "pmf-vpce-sg"
  description = "Shared VPC endpoint SG for PMF - ingress 443 from VPC CIDR"
  vpc_id      = local.vpc_id

  tags = {
    Name    = "PMF VPC Endpoints"
    Purpose = "shared-infra"
  }
}

resource "aws_security_group_rule" "vpce_ingress_443" {
  type              = "ingress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  description       = "HTTPS from any ENI in VPC"
  security_group_id = aws_security_group.vpce.id
  cidr_blocks       = [data.aws_vpc.selected.cidr_block]
}

# ---------------------------------------------------------------------------
# Interface Endpoints
# ---------------------------------------------------------------------------
# Each creates an AWS-managed ENI in every private subnet and registers the
# default public hostname (e.g. api.ecr.us-west-2.amazonaws.com) under private
# DNS so existing clients using the default hostname transparently route here.

resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.us-west-2.ecr.api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = local.private_subnet_ids
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = {
    Name    = "pmf-ecr-api"
    Purpose = "shared-infra"
  }
}

resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.us-west-2.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = local.private_subnet_ids
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = {
    Name    = "pmf-ecr-dkr"
    Purpose = "shared-infra"
  }
}

resource "aws_vpc_endpoint" "logs" {
  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.us-west-2.logs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = local.private_subnet_ids
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = {
    Name    = "pmf-cloudwatch-logs"
    Purpose = "shared-infra"
  }
}

# ---------------------------------------------------------------------------
# Gateway Endpoint (free)
# ---------------------------------------------------------------------------
# ECR image layers are stored in S3, so image pull requires an S3 path.
# Gateway endpoint adds a route table entry — no ENI, no hourly charge.

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = local.vpc_id
  service_name      = "com.amazonaws.us-west-2.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = local.route_table_ids

  tags = {
    Name    = "pmf-s3-gateway"
    Purpose = "shared-infra"
  }
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "vpce_security_group_id" {
  value       = aws_security_group.vpce.id
  description = "Endpoint SG — runner/broker egress rules reference this for 443 egress"
}

output "ecr_api_endpoint_id" {
  value = aws_vpc_endpoint.ecr_api.id
}

output "ecr_dkr_endpoint_id" {
  value = aws_vpc_endpoint.ecr_dkr.id
}

output "logs_endpoint_id" {
  value = aws_vpc_endpoint.logs.id
}

output "s3_endpoint_id" {
  value = aws_vpc_endpoint.s3.id
}
