locals {
  bucket_name = "${var.name}-${data.aws_caller_identity.current.account_id}-${var.region}"
  image_arn   = "arn:aws:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:microvm-image:${var.name}-workspace"
  connectors = [for name in ["ALL_INGRESS", "INTERNET_EGRESS"] :
    "arn:aws:lambda:${var.region}:aws:network-connector:aws-network-connector:${name}"
  ]
}

resource "aws_s3_bucket" "this" {
  bucket        = local.bucket_name
  force_destroy = false
}
resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_policy" "tls" {
  bucket = aws_s3_bucket.this.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}

# Secret values are populated by quickstart.py, never by Terraform resources/data sources.
resource "aws_secretsmanager_secret" "runtime" {
  name_prefix             = "${var.name}/runtime-"
  recovery_window_in_days = 30
}
resource "aws_db_subnet_group" "this" {
  name_prefix = "${var.name}-"
  subnet_ids  = aws_subnet.private[*].id
}
resource "aws_db_instance" "this" {
  identifier                   = var.name
  engine                       = "postgres"
  engine_version               = "17"
  instance_class               = var.database_instance_class
  allocated_storage            = 20
  max_allocated_storage        = 100
  storage_type                 = "gp3"
  storage_encrypted            = true
  db_name                      = "moyai"
  username                     = "moyai_admin"
  manage_master_user_password  = true
  db_subnet_group_name         = aws_db_subnet_group.this.name
  vpc_security_group_ids       = [aws_security_group.database.id]
  publicly_accessible          = false
  backup_retention_period      = 7
  copy_tags_to_snapshot        = true
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${var.name}-final"
  auto_minor_version_upgrade   = true
  performance_insights_enabled = true
}
resource "aws_ebs_volume" "state" {
  availability_zone = aws_subnet.private[0].availability_zone
  type              = "gp3"
  size              = 100
  encrypted         = true
  tags              = { Name = "${var.name}-state" }
}
resource "aws_backup_vault" "this" { name = var.name }
resource "aws_backup_plan" "this" {
  name = var.name
  rule {
    rule_name         = "daily-state"
    target_vault_name = aws_backup_vault.this.name
    schedule          = "cron(0 5 * * ? *)"
    lifecycle { delete_after = 7 }
  }
}
resource "aws_iam_role" "backup" {
  name_prefix = "${var.name}-backup-"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "backup.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}
resource "aws_backup_selection" "state" {
  name         = var.name
  plan_id      = aws_backup_plan.this.id
  iam_role_arn = aws_iam_role.backup.arn
  resources    = [aws_ebs_volume.state.arn]
  depends_on   = [aws_iam_role_policy_attachment.backup]
}
