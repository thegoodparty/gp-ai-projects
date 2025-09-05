#!/bin/bash
set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}📊 LLM Matching Embeddings Data Pipeline${NC}"
echo -e "${BLUE}=======================================${NC}"

# Check prerequisites
echo -e "${YELLOW}🔍 Checking prerequisites...${NC}"

if ! command -v aws &> /dev/null; then
    echo -e "${RED}❌ Error: AWS CLI not found${NC}"
    exit 1
fi

if ! aws sts get-caller-identity &> /dev/null; then
    echo -e "${RED}❌ Error: AWS credentials not configured${NC}"
    exit 1
fi

# Get S3 bucket name from SAM outputs
echo -e "${YELLOW}🔍 Getting S3 bucket name from deployment...${NC}"
S3_BUCKET=$(aws cloudformation describe-stacks --stack-name llm-matching --query 'Stacks[0].Outputs[?OutputKey==`S3BucketName`].OutputValue' --output text 2>/dev/null)

if [ -z "$S3_BUCKET" ] || [ "$S3_BUCKET" == "None" ]; then
    echo -e "${RED}❌ Error: Could not find S3 bucket from deployment. Make sure Lambda is deployed first.${NC}"
    echo -e "${YELLOW}💡 Tip: Run './deploy.sh' first to deploy the Lambda function${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Found S3 bucket: ${S3_BUCKET}${NC}"

# Look for embeddings files
echo -e "${YELLOW}🔍 Looking for embedding files...${NC}"

EMBEDDING_DIRS=(
    "../../stitch_golden_data/prod_gold_data/vector_store"
    "../../vector_store"
    "../../stitch_l2_BR_DDHQ/vector_store"
)

FOUND_DIR=""
for dir in "${EMBEDDING_DIRS[@]}"; do
    if [ -d "$dir" ] && [ -n "$(ls -A $dir/*.pkl 2>/dev/null)" ]; then
        FOUND_DIR="$dir"
        echo -e "${GREEN}✅ Found embeddings in: ${FOUND_DIR}${NC}"
        break
    fi
done

if [ -z "$FOUND_DIR" ]; then
    echo -e "${RED}❌ Error: No embedding files found in common locations:${NC}"
    for dir in "${EMBEDDING_DIRS[@]}"; do
        echo -e "${RED}   - $dir${NC}"
    done
    echo -e "${YELLOW}💡 Tip: Make sure embedding files are generated first${NC}"
    exit 1
fi

# Upload embeddings to S3
echo -e "${YELLOW}📤 Uploading embeddings to S3...${NC}"

UPLOAD_COUNT=0
TOTAL_SIZE=0

for file in "$FOUND_DIR"/l2_embeddings_*.pkl; do
    if [ -f "$file" ]; then
        filename=$(basename "$file")
        state=$(echo "$filename" | sed 's/l2_embeddings_\(.*\)\.pkl/\1/' | tr '[:lower:]' '[:upper:]')
        
        echo -e "${BLUE}  📁 Uploading $state embeddings...${NC}"
        
        # Get file size for progress
        file_size=$(stat -f%z "$file" 2>/dev/null || stat -c%s "$file" 2>/dev/null || echo "unknown")
        
        if aws s3 cp "$file" "s3://$S3_BUCKET/embeddings/$filename" --no-progress; then
            echo -e "${GREEN}     ✅ Uploaded $filename${NC}"
            UPLOAD_COUNT=$((UPLOAD_COUNT + 1))
            if [ "$file_size" != "unknown" ]; then
                TOTAL_SIZE=$((TOTAL_SIZE + file_size))
            fi
        else
            echo -e "${RED}     ❌ Failed to upload $filename${NC}"
        fi
    fi
done

# Summary
echo -e "${GREEN}🎉 Upload completed!${NC}"
echo -e "${BLUE}📊 Summary:${NC}"
echo -e "   • Files uploaded: ${UPLOAD_COUNT}"
echo -e "   • S3 bucket: ${S3_BUCKET}"

if [ $TOTAL_SIZE -gt 0 ]; then
    # Convert bytes to MB
    TOTAL_MB=$((TOTAL_SIZE / 1024 / 1024))
    echo -e "   • Total size: ~${TOTAL_MB}MB"
fi

echo -e "${YELLOW}💰 Estimated monthly S3 costs: ~$$(echo "scale=2; $TOTAL_MB * 0.023 / 1024" | bc -l 2>/dev/null || echo "0.01")${NC}"

echo -e "${BLUE}📝 Next steps:${NC}"
echo -e "   • Your Lambda function can now access embeddings from S3"
echo -e "   • Test the API with a sample request"
echo -e "   • Monitor CloudWatch logs for performance"