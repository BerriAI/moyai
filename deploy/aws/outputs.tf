output "app_url" { value = "https://${var.hostname}" }
output "instance_id" { value = aws_instance.this.id }
output "runtime_secret_arn" { value = aws_secretsmanager_secret.runtime.arn }
output "application_repository" { value = aws_ecr_repository.app.repository_url }
output "quickstart" {
  description = "Non-secret deployment inputs for quickstart.py. No secret values are read into state."
  value = {
    name                   = var.name
    region                 = var.region
    account                = data.aws_caller_identity.current.account_id
    url                    = "https://${var.hostname}"
    instance_id            = aws_instance.this.id
    bucket                 = aws_s3_bucket.this.id
    secret_arn             = aws_secretsmanager_secret.runtime.arn
    database_secret_arn    = aws_db_instance.this.master_user_secret[0].secret_arn
    database_host          = aws_db_instance.this.address
    image_arn              = local.image_arn
    image_build_role_arn   = aws_iam_role.image_builder.arn
    moyai_image            = var.moyai_image
    litellm_image          = var.litellm_image
    bedrock_model_id       = var.bedrock_model_id
    computer_lifetime      = var.computer_lifetime
    computer_idle_timeout  = var.computer_idle_timeout
    application_repository = aws_ecr_repository.app.repository_url
  }
}
