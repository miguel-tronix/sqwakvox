data "aws_iam_policy_document" "bedrock_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "sqwakvox_bedrock" {
  name               = "${var.environment}-sqwakvox-bedrock-role"
  assume_role_policy  = data.aws_iam_policy_document.bedrock_assume_role.json
  description        = "IAM role for Sqwakvox EC2 instance to invoke Bedrock models"
  tags = {
    Name        = "${var.environment}-sqwakvox-bedrock-role"
    Environment = var.environment
  }
}

data "aws_iam_policy_document" "bedrock_invoke" {
  statement {
    effect    = "Allow"
    actions   = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = var.bedrock_model_arns
  }
}

resource "aws_iam_policy" "bedrock_invoke" {
  name        = "${var.environment}-sqwakvox-bedrock-invoke-policy"
  description = "Allows invoking Bedrock foundation models for Sqwakvox"
  policy      = data.aws_iam_policy_document.bedrock_invoke.json
  tags = {
    Environment = var.environment
  }
}

resource "aws_iam_role_policy_attachment" "bedrock_invoke" {
  role       = aws_iam_role.sqwakvox_bedrock.name
  policy_arn = aws_iam_policy.bedrock_invoke.arn
}

resource "aws_iam_instance_profile" "sqwakvox_bedrock" {
  name = "${var.environment}-sqwakvox-bedrock-profile"
  role = aws_iam_role.sqwakvox_bedrock.name
  tags = {
    Environment = var.environment
  }
}
