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
    key    = "campaign-plan-lambda/dev/terraform.tfstate"
    region = "us-west-2"

    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = "us-west-2"
}

data "terraform_remote_state" "shared_slack_notifier" {
  backend = "s3"

  config = {
    bucket = "goodparty-terraform-state-us-west-2"
    key    = "shared/slack-notifier/terraform.tfstate"
    region = "us-west-2"
  }
}

module "campaign_plan_lambda" {
  source = "../../../modules/campaign-plan-lambda"

  environment                     = "dev"
  output_sqs_queue_arn            = "arn:aws:sqs:us-west-2:333022194791:develop-Queue.fifo"
  output_sqs_queue_url            = "https://sqs.us-west-2.amazonaws.com/333022194791/develop-Queue.fifo"
  shared_slack_notifier_lambda_arn = data.terraform_remote_state.shared_slack_notifier.outputs.lambda_function_arn
}

output "lambda_function_name" {
  value = module.campaign_plan_lambda.lambda_function_name
}

output "input_queue_url" {
  value = module.campaign_plan_lambda.input_queue_url
}

output "input_queue_arn" {
  value = module.campaign_plan_lambda.input_queue_arn
}

output "s3_bucket_name" {
  value = module.campaign_plan_lambda.s3_bucket_name
}
