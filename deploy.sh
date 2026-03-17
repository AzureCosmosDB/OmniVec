#!/bin/bash
# OmniVec Deployment Script

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║           OmniVec Deployment             ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"

# Check prerequisites
echo -e "\n${YELLOW}Checking prerequisites...${NC}"

command -v az >/dev/null 2>&1 || { echo -e "${RED}Azure CLI is required${NC}"; exit 1; }
command -v terraform >/dev/null 2>&1 || { echo -e "${RED}Terraform is required${NC}"; exit 1; }
command -v kubectl >/dev/null 2>&1 || { echo -e "${RED}kubectl is required${NC}"; exit 1; }
command -v helm >/dev/null 2>&1 || { echo -e "${RED}Helm is required${NC}"; exit 1; }

echo -e "${GREEN}All prerequisites met!${NC}"

# Variables
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENVIRONMENT="${ENVIRONMENT:-dev}"
LOCATION="${LOCATION:-eastus}"
PREFIX="${PREFIX:-omnivec}"

echo -e "\n${YELLOW}Configuration:${NC}"
echo "  Environment: $ENVIRONMENT"
echo "  Location: $LOCATION"
echo "  Prefix: $PREFIX"

# Step 1: Deploy Infrastructure
echo -e "\n${YELLOW}Step 1: Deploying Azure Infrastructure...${NC}"
cd "$SCRIPT_DIR/terraform"

terraform init
terraform plan -var="prefix=$PREFIX" -var="environment=$ENVIRONMENT" -var="location=$LOCATION" -out=tfplan
terraform apply tfplan

# Get outputs
ACR_LOGIN_SERVER=$(terraform output -raw acr_login_server)
AKS_CLUSTER_NAME=$(terraform output -raw aks_cluster_name)
RESOURCE_GROUP=$(terraform output -raw resource_group_name)
COSMOS_ENDPOINT=$(terraform output -raw cosmos_endpoint)
STORAGE_ACCOUNT=$(terraform output -raw storage_account_name)
STORAGE_BLOB_ENDPOINT=$(terraform output -raw storage_blob_endpoint)
SERVICEBUS_NAMESPACE=$(terraform output -raw servicebus_namespace)
WORKLOAD_IDENTITY_CLIENT_ID=$(terraform output -raw workload_identity_client_id)

echo -e "${GREEN}Infrastructure deployed!${NC}"

# Step 2: Get AKS credentials
echo -e "\n${YELLOW}Step 2: Connecting to AKS...${NC}"
az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER_NAME" --overwrite-existing

# Step 3: Login to ACR
echo -e "\n${YELLOW}Step 3: Logging into ACR...${NC}"
az acr login --name "${ACR_LOGIN_SERVER%%.*}"

# Step 4: Build and push images
echo -e "\n${YELLOW}Step 4: Building and pushing images...${NC}"

# Build OmniVec API
cd "$SCRIPT_DIR"
docker build -t "$ACR_LOGIN_SERVER/omnivec-api:v1" -f Dockerfile .
docker push "$ACR_LOGIN_SERVER/omnivec-api:v1"

# Build DocGrok (if exists)
if [ -d "$SCRIPT_DIR/../docgrok" ]; then
    cd "$SCRIPT_DIR/../docgrok"
    docker build -t "$ACR_LOGIN_SERVER/docgrok:v22" .
    docker push "$ACR_LOGIN_SERVER/docgrok:v22"
fi

echo -e "${GREEN}Images pushed!${NC}"

# Step 5: Deploy with Helm
echo -e "\n${YELLOW}Step 5: Deploying with Helm...${NC}"
cd "$SCRIPT_DIR/helm"

helm upgrade --install omnivec ./omnivec \
  --namespace omnivec \
  --create-namespace \
  --set global.imageRegistry="$ACR_LOGIN_SERVER" \
  --set azure.workloadIdentity.clientId="$WORKLOAD_IDENTITY_CLIENT_ID" \
  --set azure.cosmos.endpoint="$COSMOS_ENDPOINT" \
  --set azure.storage.accountName="$STORAGE_ACCOUNT" \
  --set azure.storage.blobEndpoint="$STORAGE_BLOB_ENDPOINT" \
  --set azure.serviceBus.namespace="${SERVICEBUS_NAMESPACE}.servicebus.windows.net" \
  --wait

echo -e "${GREEN}Deployment complete!${NC}"

# Step 6: Get service URL
echo -e "\n${YELLOW}Getting service URL...${NC}"
sleep 10
EXTERNAL_IP=$(kubectl get svc omnivec-api -n omnivec -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

echo -e "\n${GREEN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║         Deployment Successful!           ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
echo -e "\nOmniVec UI: ${YELLOW}http://$EXTERNAL_IP/ui${NC}"
echo -e "OmniVec API: ${YELLOW}http://$EXTERNAL_IP/health${NC}"
