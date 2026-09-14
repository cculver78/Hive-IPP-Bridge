#!/usr/bin/env bash
set -euo pipefail

project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
install_dir="$HOME/.local/lib/hive-ipp-bridge"
package_dir="$install_dir/hive_ipp_bridge"
bin_dir="$HOME/.local/bin"

install -d -- "$package_dir" "$bin_dir"
for source_file in "$project_root"/src/hive_ipp_bridge/*.py; do
    install -m 0644 -- "$source_file" "$package_dir/${source_file##*/}"
done
chmod 0755 -- "$package_dir/ipp_command.py" "$package_dir/submit.py"
install -m 0755 -- "$project_root/packaging/hive-ipp-bridge" "$bin_dir/hive-ipp-bridge"

echo "Installed hive-ipp-bridge in $bin_dir."
echo "Run 'hive-ipp-bridge setup' to configure printing."
