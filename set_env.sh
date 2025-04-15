#!/bin/bash

# This script is used to set git-bash environment

set -e

cat > /etc/profile.d/vmctl.sh <<'EOF'
export PATH=$PATH:/d/Downloads/repos/vm_manager
alias vm="vmctl.sh"
EOF

cp vmctl-completion.sh /etc/profile.d/
source /etc/profile
