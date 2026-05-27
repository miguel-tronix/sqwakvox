variable "aws_region" {
  description = "AWS region for Bedrock and EC2"
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.medium"
}

variable "ami_id" {
  description = "AMI ID for EC2 (if left empty, dynamically queries the latest Amazon Linux 2023 AMI for the selected region)"
  type        = string
  default     = ""
}

variable "ssh_key_name" {
  description = "Name of an existing EC2 key pair for SSH access"
  type        = string
}

variable "allowed_ssh_cidr" {
  description = "CIDR block allowed to SSH into the EC2 instance"
  type        = string
  default     = "0.0.0.0/0"
}

variable "bedrock_model_arns" {
  description = "List of Bedrock foundation model ARNs to allow InvokeModel on"
  type        = list(string)
  default = [
    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0",
    "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0",
    "arn:aws:bedrock:us-east-1::foundation-model/meta.llama3-70b-instruct-v1:0",
    "arn:aws:bedrock:us-east-1::foundation-model/mistral.mistral-large-2402-v1:0",
  ]
}

variable "repo_url" {
  description = "Git repository URL for Sqwakvox"
  type        = string
  default     = "https://github.com/your-org/sqwakvox"
}

variable "volume_size" {
  description = "EBS root volume size in GB"
  type        = number
  default     = 20
}

variable "environment" {
  description = "Deployment environment tag"
  type        = string
  default     = "dev"
}
