#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${repo_url}"

dnf update -y
dnf install -y python3.12 git tmux

git clone "$REPO_URL" /home/ec2-user/sqwakvox
cd /home/ec2-user/sqwakvox

pip3 install -e . langchain-aws --no-cache-dir

cat > /home/ec2-user/.bashrc <<'BASHRC'
alias sqwakvox='cd ~/sqwakvox && python -m sqwakvox'
export AWS_DEFAULT_REGION=${aws_region}
BASHRC

chown -R ec2-user:ec2-user /home/ec2-user/sqwakvox /home/ec2-user/.bashrc

echo "Sqwakvox setup complete. Run: sqwakvox"
