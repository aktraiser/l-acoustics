#!/bin/bash
# Deployment script for Feedly Collector Azure Function
# Usage: ./deploy.sh <function-app-name> <resource-group>

set -e

if [ $# -lt 2 ]; then
    echo "Usage: ./deploy.sh <function-app-name> <resource-group>"
    echo "Example: ./deploy.sh func-feedly-collector rg-feedly-collector"
    exit 1
fi

FUNCTION_APP_NAME=$1
RESOURCE_GROUP=$2

echo "🚀 Deploying Feedly Collector to Azure..."
echo "  Function App: $FUNCTION_APP_NAME"
echo "  Resource Group: $RESOURCE_GROUP"
echo ""

# Check if logged in to Azure
echo "Checking Azure login..."
az account show > /dev/null 2>&1 || {
    echo "Not logged in to Azure. Running 'az login'..."
    az login
}

# Check if Function App exists
echo "Checking if Function App exists..."
if ! az functionapp show --name "$FUNCTION_APP_NAME" --resource-group "$RESOURCE_GROUP" > /dev/null 2>&1; then
    echo "❌ Function App '$FUNCTION_APP_NAME' not found in resource group '$RESOURCE_GROUP'"
    echo ""
    echo "Create it first with:"
    echo "  az functionapp create \\"
    echo "    --name $FUNCTION_APP_NAME \\"
    echo "    --resource-group $RESOURCE_GROUP \\"
    echo "    --consumption-plan-location westeurope \\"
    echo "    --runtime python \\"
    echo "    --runtime-version 3.11 \\"
    echo "    --functions-version 4 \\"
    echo "    --storage-account <storage-account-name> \\"
    echo "    --os-type Linux"
    exit 1
fi

# Load environment variables from local.settings.json
if [ -f "local.settings.json" ]; then
    echo "Loading configuration from local.settings.json..."
    FEEDLY_APIM_URL=$(cat local.settings.json | grep -o '"FEEDLY_APIM_URL"[^,]*' | cut -d'"' -f4)
    AI_SEARCH_ENDPOINT=$(cat local.settings.json | grep -o '"AI_SEARCH_ENDPOINT"[^,]*' | cut -d'"' -f4)
    AI_SEARCH_INDEX=$(cat local.settings.json | grep -o '"AI_SEARCH_INDEX"[^,]*' | cut -d'"' -f4)
    AI_SEARCH_KEY=$(cat local.settings.json | grep -o '"AI_SEARCH_KEY"[^,]*' | cut -d'"' -f4)

    # Update App Settings
    echo "Updating Function App settings..."
    az functionapp config appsettings set \
        --name "$FUNCTION_APP_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --settings \
            FEEDLY_APIM_URL="$FEEDLY_APIM_URL" \
            AI_SEARCH_ENDPOINT="$AI_SEARCH_ENDPOINT" \
            AI_SEARCH_INDEX="$AI_SEARCH_INDEX" \
            AI_SEARCH_KEY="$AI_SEARCH_KEY" \
        > /dev/null

    echo "✅ App settings updated"
else
    echo "⚠️  local.settings.json not found - skipping app settings update"
    echo "   You'll need to configure them manually in the Azure Portal"
fi

# Deploy the function
echo "Deploying function code..."

# Check if func command is available
if command -v func &> /dev/null; then
    func azure functionapp publish "$FUNCTION_APP_NAME" --python
else
    echo "⚠️  Azure Functions Core Tools not found"
    echo "   Creating deployment package manually..."

    # Create a zip package
    zip -r function.zip . -x "*.git*" "*.venv*" "*__pycache__*" "*.DS_Store" "local.settings.json" "*.sh" "*.md"

    # Deploy via Azure CLI
    az functionapp deployment source config-zip \
        --name "$FUNCTION_APP_NAME" \
        --resource-group "$RESOURCE_GROUP" \
        --src function.zip

    rm function.zip
fi

echo ""
echo "✅ Deployment complete!"
echo ""
echo "Next steps:"
echo "  1. Verify logs: az functionapp log tail --name $FUNCTION_APP_NAME --resource-group $RESOURCE_GROUP"
echo "  2. Check Monitor: https://portal.azure.com → Function App → $FUNCTION_APP_NAME → Monitor"
echo "  3. Wait for next Monday 6:00 AM or trigger manually in the portal"
