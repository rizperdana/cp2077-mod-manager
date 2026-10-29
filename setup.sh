#!/bin/bash
set -e

echo "========================================="
echo "CP2077 Mod Manager - Setup Script"
echo "========================================="
echo ""

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Check if Python 3 is installed
if ! command -v python3 &> /dev/null; then
    echo "ERROR: python3 not found. Please install Python 3.9 or newer."
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
else
    echo "Virtual environment already exists."
fi

# Activate virtual environment
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install dependencies
echo "Installing dependencies..."
pip install PyQt6 py7zr rarfile

echo ""
echo "========================================="
echo "Setup complete!"
echo "========================================="
echo ""
echo "To run the mod manager, use:"
echo "  ./run_mod_manager.sh"
echo ""
