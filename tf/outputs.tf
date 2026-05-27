output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.sqwakvox.id
}

output "instance_public_ip" {
  description = "Public IP address of the EC2 instance"
  value       = aws_instance.sqwakvox.public_ip
}

output "instance_public_dns" {
  description = "Public DNS of the EC2 instance"
  value       = aws_instance.sqwakvox.public_dns
}

output "ssh_command" {
  description = "SSH command to connect to the instance"
  value       = "ssh -o ServerAliveInterval=60 ec2-user@${aws_instance.sqwakvox.public_ip}"
}

output "bedrock_models" {
  description = "Bedrock models available via the IAM policy"
  value = [
    for arn in var.bedrock_model_arns : reverse(split("/", arn))[0]
  ]
}

output "iam_role_name" {
  description = "Name of the IAM role attached to the instance"
  value       = aws_iam_role.sqwakvox_bedrock.name
}
