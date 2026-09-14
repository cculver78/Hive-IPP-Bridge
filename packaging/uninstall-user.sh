#!/usr/bin/env bash
set -euo pipefail

purge=false
case ${1:-} in
    "") ;;
    --purge|--full) purge=true ;;
    *) echo "usage: $0 [--purge|--full]" >&2; exit 2 ;;
esac

device_uri=ipp://localhost:8631/ipp/print
service_name=hive-ipp-bridge.service
service_file="$HOME/.config/systemd/user/$service_name"
install_dir="$HOME/.local/lib/hive-ipp-bridge"
package_dir="$install_dir/hive_ipp_bridge"
launcher="$HOME/.local/bin/hive-ipp-bridge"

for queue_name in Hive_IPP_Bridge PaperCut_Hive; do
    existing_uri=$(lpstat -v "$queue_name" 2>/dev/null | sed -n 's/^device for [^:]*: //p' || true)
    if [[ -n $existing_uri ]]; then
        if [[ $existing_uri != "$device_uri" ]]; then
            echo "error: $queue_name points to $existing_uri, not $device_uri; leaving it untouched" >&2
            exit 1
        fi
        lpadmin -x "$queue_name"
        echo "Removed CUPS queue $queue_name."
    fi
done

for service_name in hive-ipp-bridge.service papercut-hive-printer.service; do
    service_file="$HOME/.config/systemd/user/$service_name"
    systemctl --user disable --now "$service_name" >/dev/null 2>&1 || true
    if [[ -f $service_file ]]; then
        rm -- "$service_file"
        systemctl --user daemon-reload
        echo "Removed user service $service_name."
    fi
done

for file in __init__.py __main__.py cli.py enrollment.py ipp_command.py submit.py; do
    file="$package_dir/$file"
    [[ ! -f $file ]] || rm -- "$file"
done
rmdir -- "$package_dir/__pycache__" 2>/dev/null || true
rmdir -- "$package_dir" 2>/dev/null || true
rmdir -- "$install_dir" 2>/dev/null || true
if [[ -f $launcher ]] && grep -q 'HIVE_IPP_BRIDGE_LAUNCHER=1' "$launcher"; then
    rm -- "$launcher"
fi

if $purge; then
    for key in jwt client-id org-id; do
        secret-tool clear application hive-ipp-bridge key "$key" >/dev/null 2>&1 || true
    done
    echo "Removed Hive IPP Bridge keyring credentials."
else
    echo "Keyring credentials were preserved. Use --purge to remove them."
fi

echo "Hive IPP Bridge user installation removed."
