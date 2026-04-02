#!/bin/bash
# Setup script for Splunk MCP Edge password storage
# This extracts the password from macOS Keychain ONCE and stores it securely

set -e

echo "=== Splunk MCP Edge Password Setup ==="
echo ""
echo "This script will extract your Microsoft Edge Safe Storage password"
echo "from macOS Keychain and store it securely for automated access."
echo ""
echo "Choose a storage method:"
echo "  1) Environment variable (add to ~/.bashrc or ~/.zshrc)"
echo "  2) Plain text file with chmod 600 (recommended)"
echo "  3) GPG encrypted file (most secure)"
echo ""
read -p "Enter choice [1-3]: " choice

# Extract password from keychain (this will prompt once)
echo ""
echo "Extracting password from keychain (you may need to approve access)..."
PASSWORD=$(security find-generic-password -s "Microsoft Edge Safe Storage" -a "Microsoft Edge" -w)

if [ -z "$PASSWORD" ]; then
    echo "Error: Could not retrieve password from keychain"
    exit 1
fi

echo "Password retrieved successfully!"
echo ""

case $choice in
    1)
        echo "Add this line to your ~/.bashrc or ~/.zshrc:"
        echo ""
        echo "export EDGE_SAFE_STORAGE_PASSWORD='$PASSWORD'"
        echo ""
        echo "Then run: source ~/.bashrc  (or source ~/.zshrc)"
        ;;
    2)
        mkdir -p ~/.splunk-mcp
        echo "$PASSWORD" > ~/.splunk-mcp/edge-password
        chmod 600 ~/.splunk-mcp/edge-password
        echo "Password saved to: ~/.splunk-mcp/edge-password"
        echo "File permissions set to 600 (owner read/write only)"
        echo ""
        echo "Verification:"
        ls -la ~/.splunk-mcp/edge-password
        ;;
    3)
        read -p "Enter your GPG email/key ID: " GPG_KEY
        mkdir -p ~/.splunk-mcp
        echo "$PASSWORD" | gpg --encrypt --recipient "$GPG_KEY" > ~/.splunk-mcp/edge-password.gpg
        echo "Password encrypted and saved to: ~/.splunk-mcp/edge-password.gpg"
        echo ""
        echo "Test decryption:"
        gpg --decrypt --quiet ~/.splunk-mcp/edge-password.gpg
        ;;
    *)
        echo "Invalid choice"
        exit 1
        ;;
esac

echo ""
echo "✓ Setup complete!"
echo ""
echo "The splunk-mcp.py script will now run without keychain prompts."
