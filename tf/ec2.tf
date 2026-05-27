resource "aws_security_group" "sqwakvox" {
  name        = "${var.environment}-sqwakvox-sg"
  description = "Security group for Sqwakvox EC2 instance"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH from allowed CIDR"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  egress {
    description = "All outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "${var.environment}-sqwakvox-sg"
    Environment = var.environment
  }
}

resource "aws_instance" "sqwakvox" {
  ami                  = var.ami_id
  instance_type        = var.instance_type
  key_name             = var.ssh_key_name
  iam_instance_profile = aws_iam_instance_profile.sqwakvox_bedrock.name
  vpc_security_group_ids = [aws_security_group.sqwakvox.id]

  root_block_device {
    volume_type = "gp3"
    volume_size = var.volume_size
    tags = {
      Name        = "${var.environment}-sqwakvox-root"
      Environment = var.environment
    }
  }

  user_data = templatefile("${path.module}/user_data.sh.tpl", {
    repo_url    = var.repo_url
    aws_region  = var.aws_region
  })

  user_data_replace_on_change = false

  tags = {
    Name        = "${var.environment}-sqwakvox"
    Environment = var.environment
  }
}

data "aws_vpc" "default" {
  default = true
}
