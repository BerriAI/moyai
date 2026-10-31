resource "aws_iam_role" "host" {
  name_prefix = "${var.name}-host-"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_instance_profile" "host" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.host.name
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "host" {
  role = aws_iam_role.host.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation"], Resource = aws_s3_bucket.this.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.this.arn}/moyai/*" },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.this.arn}/deployments/*" },
    { Effect = "Allow", Action = ["lambda:RunMicrovm", "lambda:GetMicrovm", "lambda:ResumeMicrovm", "lambda:TerminateMicrovm", "lambda:CreateMicrovmAuthToken", "lambda:GetMicrovmImageVersion"], Resource = local.image_arn },
    { Effect = "Allow", Action = "lambda:PassNetworkConnector", Resource = local.connectors },
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = [aws_secretsmanager_secret.runtime.arn, aws_db_instance.this.master_user_secret[0].secret_arn] },
    { Effect = "Allow", Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], Resource = var.bedrock_invoke_resources },
    { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = aws_ecr_repository.app.arn },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.app.arn}:*" }
  ] })
}
resource "aws_iam_role" "image_builder" {
  name_prefix = "${var.name}-image-"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"]
  }] })
}
resource "aws_iam_role_policy" "image_builder" {
  role = aws_iam_role.image_builder.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.this.arn}/moyai-image-builds/*" },
    { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda-microvms/${var.name}-workspace*" }
  ] })
}
