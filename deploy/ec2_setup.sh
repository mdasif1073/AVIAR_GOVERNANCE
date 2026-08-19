#!/usr/bin/env bash
# ==============================================================================
# AIVAR Agent Budget Controller - AWS EC2 Automated Deployment Script
# Target: Ubuntu 22.04 LTS / 24.04 LTS (AWS EC2 t2.micro / t3.micro Free Tier)
# ==============================================================================

set -e

echo "🚀 [1/5] Updating system packages..."
sudo apt-get update -y && sudo apt-get upgrade -y

echo "🐳 [2/5] Installing Docker & Docker Compose..."
sudo apt-get install -y ca-certificates curl gnupg lsb-release
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update -y
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

sudo usermod -aG docker $USER

echo "📦 [3/5] Building & Launching AIVAR Budget Gateway Container..."
sudo docker compose down || true
sudo docker compose up --build -d

echo "⏳ [4/5] Waiting for Gateway Healthcheck..."
sleep 5
curl -f http://localhost:8000/health || (echo "Healthcheck failed! Check logs: sudo docker compose logs" && exit 1)

echo "✅ [5/5] Deployment Successful!"
echo "------------------------------------------------------------------"
echo "🌐 Web Dashboard: http://$(curl -s http://checkip.amazonaws.com):8000"
echo "📚 API Docs (Swagger): http://$(curl -s http://checkip.amazonaws.com):8000/docs"
echo "📊 Prometheus Metrics: http://$(curl -s http://checkip.amazonaws.com):8000/metrics"
echo "------------------------------------------------------------------"
