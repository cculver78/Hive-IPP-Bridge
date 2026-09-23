# Hive IPP Bridge

Hive IPP Bridge is an independent, unofficial Linux and Windows compatibility
client that submits local IPP print jobs to PaperCut Hive. It is not affiliated with,
endorsed by, or supported by PaperCut Software.

Developed and maintained by [Edge Case Software](https://edgecasesoftware.dev).

The Linux implementation uses a user-level CUPS queue and systemd service. The
Windows implementation uses the Windows Print Spooler, the Microsoft IPP Class
Driver, a LocalService runtime, and a credential-blind provisioning service.
Both platforms use the same independent enrollment and Hive job-submission
implementation.

The project code is released under the [MIT License](LICENSE). The license does not
grant rights to PaperCut trademarks, logos, extension code, or proprietary assets.

## Linux command-line usage

```text
hive-ipp-bridge setup
hive-ipp-bridge status
hive-ipp-bridge test document.pdf
hive-ipp-bridge uninstall
```

`setup` saves client credentials in a Secret Service-compatible desktop keyring,
starts a user-level IPP printer, and creates one idempotent CUPS queue. This is
not tied to a particular desktop environment: GNOME Keyring, KDE's Secret Service
provider, or another compatible provider can supply the keyring. On lightweight,
custom, or headless sessions, the user must install and start a compatible provider
on the user D-Bus session. Plaintext credential-file fallback is intentionally not
supported.

On first setup, the command prompts for the PaperCut Hive setup link, enrolls
with PaperCut, generates a unique client ID, and stores the resulting
credentials. Existing prototype installations can instead provide
`PAPERCUT_HIVE_JWT`, `PAPERCUT_HIVE_CLIENT_ID`, and `PAPERCUT_HIVE_ORG_ID`
together for migration. Later setup runs do not need the link or variables.

`status` checks the credentials, user service, and CUPS queue. `test` submits the
specified PDF directly and reports whether PaperCut Hive accepted it. `uninstall`
removes the per-user service and queue while preserving credentials by default;
use `hive-ipp-bridge uninstall --purge` to remove the credentials as well.

To remove everything installed for the user, run:

```bash
hive-ipp-bridge uninstall --full
```

Full removal also deletes the installed Python files and the managed
`~/.local/bin/hive-ipp-bridge` launcher.

For upgrades from the working prototype, uninstall also recognizes the legacy
`PaperCut_Hive` queue and removes it only when it uses the expected bridge URI.

## Windows support

Windows 10 and Windows 11 x64 are supported. The packaged Windows application
does not require Python or pip on user workstations. See the complete
[Windows setup and user deployment guide](General/WINDOWS_SETUP.md) for
prerequisites, upgrades, verification, security details, and troubleshooting.

The Windows path is:

```text
Windows Print Spooler
  -> Microsoft IPP Class Driver
  -> private per-user http://127.0.0.1:8631/ipp/<route>/print
  -> Hive IPP Bridge Windows service
  -> the signed-in user's PaperCut Hive profile
```

The administrator installs the machine portion once. Each signed-in user then
enrolls without UAC and receives a separate SID-owned **Hive IPP Bridge
(user-id)** printer, private route, and DPAPI-protected PaperCut profile.

### Windows quick start

Build the deployment bundle on Windows:

```powershell
.\packaging\windows\build-installer.ps1
```

Copy these two resulting files together:

```text
dist\installer\HiveIPPBridge-Setup.exe
dist\installer\install-system.ps1
```

An administrator runs the system installation once:

```powershell
.\install-system.ps1
```

Then each user signs in and runs this normally, not as administrator:

```powershell
& "$env:ProgramFiles\Hive IPP Bridge\Install-User.ps1"
```

Each user needs their own PaperCut Hive Classic Invitation setup link.
For remote deployment tools such as PDQ Deploy running as the logged-on user,
pass the link as `-InviteLink "https://hive.papercut.com/setup-instructions?t=..."`
to `Install-User.ps1` instead of responding to the prompt.

### Windows verification

From the user's normal PowerShell session:

```powershell
& "$env:ProgramFiles\Hive IPP Bridge\HiveIPPBridge.exe" status
& "$env:ProgramFiles\Hive IPP Bridge\HiveIPPBridge.exe" test .\document.pdf
```

The machine has two automatic services. `HiveIPPBridge` runs as LocalService
and owns the encrypted multi-user vault and localhost IPP listener.
`HiveIPPBridgeProvisioner` is a credential-blind LocalSystem broker restricted
to managing verified per-user queues. PaperCut credentials never go to the
provisioner, printer URLs, command-line arguments, environment variables, or
logs.

### Windows removal

Remove only the signed-in user's queue and profile without UAC:

```powershell
& "$env:ProgramFiles\Hive IPP Bridge\Uninstall-User.ps1"
```

Remove the services, all managed queues, and installed files with one UAC
prompt while preserving encrypted profiles for reinstall:

```powershell
& "$env:ProgramFiles\Hive IPP Bridge\Uninstall-System.ps1"
```

Add `-PurgeUserData` to permanently remove all encrypted profiles as well.

### Windows build

The combined script requires 64-bit CPython 3.12 or newer and Inno Setup 7 x64
or Inno Setup 6. It runs tests, builds the PyInstaller onedir application under
`dist\HiveIPPBridge`, compiles `HiveIPPBridge-Setup.exe`, and places the system
install script beside it. PyInstaller is not a cross-compiler; build Windows
artifacts on Windows.

The project remains an independent interoperability implementation and must
not include PaperCut proprietary source, extensions, icons, certificates,
credentials, or captured network artifacts.

## Linux first-time installation

After downloading and extracting a release, run its top-level installer:

```bash
./install.sh
```

This single command installs `hive-ipp-bridge` in `~/.local/bin` and immediately
starts interactive setup. No root access or Python package manager is required.
Once setup completes, a CUPS printer named **Hive_IPP_Bridge** appears in print
dialogs. Afterward, use `hive-ipp-bridge setup` to repair or repeat
configuration. Package manager installations will install the command directly
and will not use this bootstrap script.

## Obtaining the PaperCut Hive setup link

During first-time setup, you will be prompted for a setup link. An administrator
sends this to the user via a Classic Invitation email from the PaperCut Hive
admin console:

1. Sign in to the PaperCut Hive admin console (for example,
   `https://hive.papercut.com` or your region's equivalent).
2. Go to **Edge Mesh** and click **Add Edge Nodes**.
3. Under "Let your users do the work", click **Invite a User**.
4. Select the **Email** tab and enter the user's email address.
5. Click **Customize user's email invitation**.
6. Change the **User invitation type** to **Send a Classic Invitation**.
7. Click **Send Invite**.

The user receives an email with a green **Get Started** button. Right-click the
button and copy the link. It looks like
`https://hive.papercut.com/setup-instructions?t=eyJ…`.

When running Linux `hive-ipp-bridge setup` or Windows `Install-User.ps1`, paste
the copied link at the prompt.

Each setup link is tied to a specific user. If enrollment fails or the link
expires, send a new Classic Invitation from the admin console and rerun the
platform's user setup command.

If you are not a PaperCut Hive administrator, ask your IT administrator to
send you a Classic Invitation email.

See [TRADEMARKS.md](TRADEMARKS.md) and [PROVENANCE.md](PROVENANCE.md) before
publishing or redistributing.
