#!/usr/bin/env bash
set -euo pipefail

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

"$project_root/packaging/install-user.sh"
exec "$HOME/.local/bin/hive-ipp-bridge" setup
