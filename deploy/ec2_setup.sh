#!/usr/bin/env bash
# ==============================================================================
# AIVAR Agent Budget Controller — AWS EC2 Deployment Script
# Ubuntu 22.04 LTS / t2.micro or t3.micro (Free Tier)
# Run this ONCE on a fresh EC2 instance to deploy the full production stack.
# ==============================================================================

set -e

REPO_URL="https://github.com/mdasif1073/AVIAR_GOVERNANCE.git"
APP_DIR="$HOME/AVIAR_GOVERNANCE"

echo "════════════════════════════════════════════════════════════"
echo "  AIVAR Budget Controller — Production EC2 Deployment"
echo "════════════════════════════════════════════════════════════"

# 1. System update
echo "→ [1/6] Updating system packages..."
sudo apt-get update -y && sudo apt-get upgrade -y -q

# 2. Install Docker
echo "→ [2/6] Installing Docker..."
sudo apt-get install -y -q ca-certificates curl gnupg lsb-release
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
     https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
     | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update -y -q
sudo apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"

# 3. Clone or update the repo
echo "→ [3/6] Cloning repository..."
if [ -d "$APP_DIR" ]; then
  cd "$APP_DIR" && git pull origin main
else
  git clone "$REPO_URL" "$APP_DIR" && cd "$APP_DIR"
fi

cd "$APP_DIR"

# 4. Write .env from environment variables passed to this script
echo "→ [4/6] Writing .env configuration..."
cat > .env << EOF
GROQ_API_KEY=${GROQ_API_KEY}
AWS_REGION=${AWS_REGION:-us-east-1}
AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}
AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}
DYNAMODB_ENDPOINT_URL=
APP_ENV=production
DEBUG=false
PORT=8000
WARN_THRESHOLD_PERCENT=80.0
HARD_BLOCK_THRESHOLD_PERCENT=100.0
RUNAWAY_VELOCITY_PERCENT=20.0
EOF

# 5. Build and launch Docker container
echo "→ [5/6] Building & launching Docker container..."
sudo docker compose down --remove-orphans 2>/dev/null || true
sudo docker compose up --build -d

# 6. Health check
echo "→ [6/6] Waiting for gateway to be ready..."
sleep 8
PUBLIC_IP=$(curl -s --max-time 5 http://checkip.amazonaws.com 2>/dev/null || echo "localhost")
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:8000/health" 2>/dev/null || echo "000")

echo ""
echo "════════════════════════════════════════════════════════════"
if [ "$HTTP_CODE" = "200" ]; then
  echo "  ✅  DEPLOYMENT SUCCESSFUL!"
  echo ""
  echo "  🌐  Dashboard:  http://${PUBLIC_IP}:8000"
  echo "  📚  API Docs:   http://${PUBLIC_IP}:8000/docs"
  echo "  📊  Metrics:    http://${PUBLIC_IP}:8000/metrics"
  echo "  ❤️   Health:     http://${PUBLIC_IP}:8000/health"
else
  echo "  ⚠️   Container started but healthcheck returned HTTP $HTTP_CODE"
  echo "  Check logs with: sudo docker compose logs"
fi
echo "════════════════════════════════════════════════════════════"
