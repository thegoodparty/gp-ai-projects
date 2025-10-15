resource "aws_dynamodb_table" "campaign_data" {
  name           = var.table_name
  billing_mode   = "PAY_PER_REQUEST"  # On-demand pricing for flexibility
  hash_key       = "campaign_id"
  range_key      = "record_id"

  attribute {
    name = "campaign_id"
    type = "S"
  }

  attribute {
    name = "record_id"
    type = "S"
  }

  tags = {
    Name        = "Campaign Data Table"
    Environment = var.environment
    Project     = "GoodParty Campaign Platform"
  }

  # Enable point-in-time recovery
  point_in_time_recovery {
    enabled = true
  }

  # Enable server-side encryption
  server_side_encryption {
    enabled = true
  }
}