#!/bin/bash

# This script is used to set git-bash environment

set -e

SCRIPT_DIR=$(realpath "$(dirname "$0")")

cat > /etc/profile.d/vmctl.sh <<EOF
export PATH=\$PATH:$SCRIPT_DIR
alias vm="vmctl.sh"
EOF

cp vmctl-completion.sh /etc/profile.d/
source /etc/profile
