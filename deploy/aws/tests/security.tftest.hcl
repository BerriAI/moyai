mock_provider "aws" {}

override_data {
  target = data.aws_caller_identity.current
  values = { account_id = "123456789012" }
}
override_data {
  target = data.aws_availability_zones.available
  values = { names = ["us-east-1a", "us-east-1b"] }
}
override_data {
  target = data.aws_route53_zone.selected
  values = { name = "example.com.", private_zone = false }
}

run "private_host_and_durable_defaults" {
  command = plan
  variables {
    hostname                 = "moyai.example.com"
    route53_zone_id          = "ZEXAMPLE"
    deletion_protection      = true
    bedrock_invoke_resources = ["arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6"]
  }
  assert {
    condition     = !aws_instance.this.associate_public_ip_address && !aws_db_instance.this.publicly_accessible
    error_message = "The application host and database must not have public endpoints."
  }
  assert {
    condition     = aws_instance.this.metadata_options[0].http_tokens == "required" && aws_ebs_volume.state.encrypted && aws_db_instance.this.storage_encrypted
    error_message = "Require IMDSv2 and encrypted durable storage."
  }
  assert {
    condition     = aws_db_instance.this.manage_master_user_password && aws_db_instance.this.password == null
    error_message = "RDS must manage its password; Terraform must not receive it."
  }
  assert {
    condition     = aws_db_instance.this.deletion_protection && !aws_db_instance.this.skip_final_snapshot && !aws_s3_bucket.this.force_destroy
    error_message = "Default teardown must protect durable data."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.this.block_public_policy && aws_s3_bucket_public_access_block.this.restrict_public_buckets && aws_s3_bucket_versioning.this.versioning_configuration[0].status == "Enabled"
    error_message = "Artifacts/checkpoints must be private and versioned."
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.https.from_port == 443 && aws_vpc_security_group_ingress_rule.app.from_port == 8787 && aws_vpc_security_group_ingress_rule.database.from_port == 5432
    error_message = "Only HTTPS, the ALB-to-app port and the app-to-database port are admitted."
  }
}
