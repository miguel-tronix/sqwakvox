#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${repo_url}"

# Update package index and install required system packages
dnf update -y
dnf install -y python3.12 python3.12-pip git tmux

# Create app directory and clone repository as ec2-user
mkdir -p /home/ec2-user/sqwakvox
git clone "$REPO_URL" /home/ec2-user/sqwakvox
chown -R ec2-user:ec2-user /home/ec2-user/sqwakvox

# Run python environment setup as ec2-user to avoid root-owned caches and PEP 668 constraints
sudo -u ec2-user -i bash -c "
  cd /home/ec2-user/sqwakvox
  python3.12 -m venv /home/ec2-user/venv
  source /home/ec2-user/venv/bin/activate
  pip install --upgrade pip
  pip install -e . langchain-aws --no-cache-dir
"

# Configure shell environment and aliases for ec2-user
cat >> /home/ec2-user/.bashrc <<BASHRC

# Sqwakvox configuration
alias sqwakvox='cd ~/sqwakvox && source ~/venv/bin/activate && python3 -m sqwakvox'
export AWS_DEFAULT_REGION=${aws_region}
BASHRC

echo "Sqwakvox setup complete. SSH into the instance and run: sqwakvox"
