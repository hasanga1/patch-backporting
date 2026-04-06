#!/bin/bash
# Setup script for Java example repository
# This script initializes the git repository and generates the required commit hashes

set -e

JAVA_REPO_DIR="dataset/java-example"
PATCH_FILE="patch_dataset/java-example/JAVA-EXAMPLE-CVE-2024-99999/fix.patch"
CONFIG_FILE="src/java-example.yml"

echo "Setting up Java example repository..."

# Check if repository already exists
if [ -d "$JAVA_REPO_DIR/.git" ]; then
    echo "Repository already initialized. Skipping git init."
else
    echo "Initializing git repository..."
    cd "$JAVA_REPO_DIR"
    git init
    git config user.email "test@example.com"
    git config user.name "Test User"
    cd - > /dev/null
fi

# Create initial commit with vulnerable code
echo "Creating initial commit..."
cd "$JAVA_REPO_DIR"

# Check if we already have commits
if git rev-parse HEAD > /dev/null 2>&1; then
    echo "Repository already has commits. Skipping initial commit."
    PARENT_HASH=$(git rev-parse HEAD)
else
    git add .
    git commit -m "Initial vulnerable version"
    PARENT_HASH=$(git rev-parse HEAD)
fi

echo "Parent commit: $PARENT_HASH"

# Apply the fix patch and create fix commit
echo "Applying fix patch..."
git am "../../$PATCH_FILE" || {
    echo "Patch application failed. You may need to manually resolve conflicts and complete the commit."
    echo "Use: git am --abort to cancel, or fix conflicts and run: git am --continue"
    cd - > /dev/null
    exit 1
}

FIX_HASH=$(git rev-parse HEAD)
TARGET_HASH=$PARENT_HASH

echo ""
echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo ""
echo "Update src/java-example.yml with these values:"
echo "  new_patch: $FIX_HASH"
echo "  new_patch_parent: $PARENT_HASH"
echo "  target_release: $TARGET_HASH"
echo ""
echo "Configuration template location: $CONFIG_FILE"
echo ""
echo "Next steps:"
echo "1. Edit $CONFIG_FILE and replace the placeholders with the above values"
echo "2. Set your OpenAI/Azure credentials"
echo "3. Run: python src/backporting.py --config src/java-example.yml --debug"
echo ""

cd - > /dev/null
