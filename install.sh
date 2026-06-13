#!/usr/bin/env bash
# install.sh — copy provider_balance.py to ~/bin and set up config
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/bin"
DEST="$BIN_DIR/provider_balance.py"
ENV_FILE="$HOME/.see-balance.env"

# ── 1. ~/bin ──────────────────────────────────────────────────────────────────
mkdir -p "$BIN_DIR"
cp "$SCRIPT_DIR/provider_balance.py" "$DEST"
chmod +x "$DEST"
echo "✓ Installed → $DEST"

# ── 2. ~/.see-balance.env (skip if already exists) ───────────────────────────
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
# see-balance config
# Get your key at: https://platform.deepseek.com/api_keys
DEEPSEEK_API_KEY=sk-your-key-here

# Optional proxy (if needed for API access)
# HTTPS_PROXY=http://127.0.0.1:7890
EOF
  echo "✓ Created config → $ENV_FILE  (edit to add your DEEPSEEK_API_KEY)"
else
  echo "✓ Config already exists → $ENV_FILE"
fi

# ── 3. Shell alias hint ───────────────────────────────────────────────────────
echo ""
echo "Add this alias to your ~/.zshrc or ~/.bashrc:"
echo ""
echo "  alias see=\"python3 ~/bin/provider_balance.py --watch 15\""
echo ""
echo "Then run:  source ~/.zshrc"
echo ""
echo "Quick test:  python3 $DEST"
