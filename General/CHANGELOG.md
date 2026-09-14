# Changelog

- Ensure the printer service and CUPS queue are running and ready before prompting for enrollment credentials during setup.
- Add socket readiness check to eliminate the `Host is down` race condition with `lpadmin`.
- Automatically decommission legacy `papercut-hive-printer.service` during setup and uninstallation.
