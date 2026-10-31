data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}
resource "aws_cloudwatch_log_group" "app" {
  name              = "/moyai/${var.name}"
  retention_in_days = 14
}
resource "aws_ecr_repository" "app" {
  name                 = "${var.name}/moyai"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}
resource "aws_instance" "this" {
  ami                         = data.aws_ssm_parameter.ami.value
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.private[0].id
  associate_public_ip_address = false
  vpc_security_group_ids      = [aws_security_group.host.id]
  iam_instance_profile        = aws_iam_instance_profile.host.name
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
  root_block_device {
    volume_type = "gp3"
    volume_size = 30
    encrypted   = true
  }
  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    volume_id = aws_ebs_volume.state.id
    region    = var.region
    log_group = aws_cloudwatch_log_group.app.name
  })
  tags       = { Name = var.name }
  depends_on = [aws_route.nat, aws_route_table_association.private, aws_iam_role_policy.host, aws_iam_role_policy_attachment.ssm]
}
resource "aws_volume_attachment" "state" {
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.state.id
  instance_id                    = aws_instance.this.id
  stop_instance_before_detaching = true
}
resource "aws_lb_target_group_attachment" "this" {
  target_group_arn = aws_lb_target_group.this.arn
  target_id        = aws_instance.this.id
  port             = 8787
}
