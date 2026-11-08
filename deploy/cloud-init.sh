#!/bin/bash
# Paste into Vultr's "Cloud-Init User-Data" when deploying an Ubuntu 24.04 instance.
# Replace CHANGE_ME with a long random string; it guards the one-time /setup page.
export SSA_SETUP_TOKEN=CHANGE_ME
curl -fsSL https://raw.githubusercontent.com/jskinner-bizai/signed-scope-agent/main/deploy/install.sh | bash > /var/log/ssa-install.log 2>&1
