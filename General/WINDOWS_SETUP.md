# Windows setup and classroom deployment

Hive IPP Bridge supports Windows 10 and Windows 11 x64. The Windows design is
intended for shared classroom computers: an administrator installs the machine
components once, and each signed-in user enrolls independently without UAC.
No user prints through another user's PaperCut Hive credentials.

## Requirements

The computer needs:

- Windows 10 or Windows 11 x64;
- the Windows Print Spooler enabled and running;
- the built-in Microsoft IPP Class Driver;
- access to the applicable PaperCut Hive cloud endpoint; and
- one PaperCut Hive Classic Invitation setup link for each user.

Python, pip, and Inno Setup are needed only on the computer that builds the
installer. They are not needed on classroom computers.

## Deployment roles

The machine and user installations are deliberately separate:

| Task | Who runs it | Elevation | Frequency |
| --- | --- | --- | --- |
| Build installer | Developer or packager | No | For each release |
| Install system portion | Administrator or IT deployment | One UAC prompt | Once per computer or upgrade |
| Install user portion | The signed-in classroom user | No | Once per Windows user |
| Uninstall user portion | The signed-in classroom user | No | When removing that user's profile |
| Uninstall system portion | Administrator or IT deployment | One UAC prompt | Once per computer |

Do not run a user install or user uninstall from an elevated Administrator
window. The Windows SID of the process is the identity used for enrollment.

## Build the Windows installer

Install 64-bit CPython 3.12 or newer and Inno Setup 7 x64 or Inno Setup 6. From
the repository root, run:

```powershell
.\packaging\windows\build-installer.ps1
```

The script runs the tests, creates the PyInstaller onedir bundle, and compiles
the Inno Setup installer. The deployable files are:

```text
dist\installer\HiveIPPBridge-Setup.exe
dist\installer\install-system.ps1
```

Keep those two files together when copying them to a classroom computer.
PyInstaller is not a cross-compiler, so this build must run on Windows.

## Install the system portion

From the folder containing the two deployment files, run:

```powershell
.\install-system.ps1
```

Accept the single UAC prompt. The script performs a silent machine
installation and verifies both services:

- `HiveIPPBridge`, running as `NT AUTHORITY\LocalService`; and
- `HiveIPPBridgeProvisioner`, running as LocalSystem.

The administrator is not enrolled by this script. The provisioner is narrowly
limited to creating and removing verified Hive queues and never receives
PaperCut credentials.

To verify the machine installation manually:

```powershell
Get-Service HiveIPPBridge,HiveIPPBridgeProvisioner |
    Format-Table Name,Status,StartType

Test-NetConnection 127.0.0.1 -Port 8631
```

Both services should be `Running` and `Automatic`, and the TCP test should
succeed.

## Install each user's portion

Sign in as the classroom user who will print. Open a normal, non-elevated
PowerShell window and run:

```powershell
& "$env:ProgramFiles\Hive IPP Bridge\Install-User.ps1"
```

Paste that user's PaperCut Hive Classic Invitation setup link when prompted.
The script will:

1. enroll the current Windows SID with that user's PaperCut account;
2. store the profile in the service-owned DPAPI vault;
3. assign a private localhost IPP route;
4. ask the provisioner to create an owner-only Microsoft IPP Class Driver
   queue; and
5. run a status verification.

The printer is named similarly to:

```text
Hive IPP Bridge (alice-a1b2c3d4)
```

The suffix is derived from the Windows SID so accounts with similar names do
not collide. Repeat this section after signing in as every classroom user.
Running the script again is safe: usable credentials are reused and the queue
is verified or repaired without requesting another setup link.

If PowerShell execution policy blocks the installed script, run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  "$env:ProgramFiles\Hive IPP Bridge\Install-User.ps1"
```

## Verify a user

Run these commands from that user's normal PowerShell session:

```powershell
& "$env:ProgramFiles\Hive IPP Bridge\HiveIPPBridge.exe" status

Get-Printer |
    Where-Object Name -Like "Hive IPP Bridge*" |
    Format-Table Name,DriverName,PortName
```

Status should report ready credentials, both running services, a ready local
IPP listener, and a ready Windows printer using `Microsoft IPP Class Driver`.
The private route token is intentionally not printed.

To test PaperCut Hive submission directly with a PDF:

```powershell
& "$env:ProgramFiles\Hive IPP Bridge\HiveIPPBridge.exe" test .\document.pdf
```

After that succeeds, print normally to the user's **Hive IPP Bridge (...)**
printer from a Windows application.

## Multi-user credential isolation

`HiveIPPBridge` authenticates control-pipe clients using their Windows SID.
Each SID has a separate encrypted PaperCut profile and random IPP route. Each
printer queue grants print access only to its owner SID, SYSTEM, and
Administrators. Job identifiers and status are also isolated by route.

The LocalSystem provisioner reads only a restricted SID-to-route registry. It
cannot decrypt the LocalService DPAPI credential vault. Setup links, JWTs,
PaperCut client credentials, and authorization headers are not placed in
printer URLs, command-line arguments, environment variables, or logs.

The listener binds only to `127.0.0.1:8631`. Port 9265 is not used. Local
administrators retain their normal ability to administer the computer and its
printer configuration.

## Upgrade an existing installation

Build or obtain the new installer and run `install-system.ps1` again. The
services and installed binaries are updated and restarted. Existing multi-user
profiles are preserved.

When upgrading from the earlier single-user Windows build, its profile is
migrated only to the original enrolled SID. The old shared **Hive IPP Bridge**
queue is removed only if its URI and driver match the bridge's legacy
configuration. Each user should then run `Install-User.ps1` once to create or
verify their private queue.

## Remove one user's portion

Sign in as the user being removed and run this without elevation:

```powershell
& "$env:ProgramFiles\Hive IPP Bridge\Uninstall-User.ps1"
```

This removes only the current user's verified printer and encrypted PaperCut
profile. It does not affect other users or either machine service.

## Remove the system portion

Run:

```powershell
& "$env:ProgramFiles\Hive IPP Bridge\Uninstall-System.ps1"
```

Accept the single UAC prompt. The script verifies and removes all managed user
queues, both services, and the installed program. Encrypted profiles under
`%ProgramData%\Hive IPP Bridge` are preserved for a future reinstall.

To permanently remove every encrypted user profile as part of system removal:

```powershell
& "$env:ProgramFiles\Hive IPP Bridge\Uninstall-System.ps1" -PurgeUserData
```

The removal code refuses to delete a printer whose driver or localhost URI does
not match the managed Hive configuration.

## Troubleshooting

### User setup asks for UAC

Do not run `HiveIPPBridge.exe install` for each user. IT runs
`install-system.ps1` once; users run `Install-User.ps1` normally. The user
script detects an elevated window and stops rather than enrolling the wrong
SID.

### A machine service is missing or stopped

Run as an administrator:

```powershell
Get-Service HiveIPPBridge,HiveIPPBridgeProvisioner
& "$env:ProgramFiles\Hive IPP Bridge\HiveIPPBridge.exe" install
```

Then retry the user script from the user's non-elevated session.

### The IPP listener is unavailable

Check the runtime service and port:

```powershell
Get-Service HiveIPPBridge
Test-NetConnection 127.0.0.1 -Port 8631
```

Another process using port 8631 must be stopped or reconfigured before the
bridge can start.

### Printer creation reports that the IPP device was not found

Confirm the runtime service and port are ready, then restart both services from
an elevated PowerShell window and rerun the user setup normally:

```powershell
Restart-Service HiveIPPBridge,HiveIPPBridgeProvisioner
Test-NetConnection 127.0.0.1 -Port 8631
```

Also confirm the built-in `Microsoft IPP Class Driver` is present and the Print
Spooler is running.

### Setup asks for another PaperCut link

Credentials are scoped to the current Windows SID. A different Windows user is
expected to receive a separate prompt. If the same user is prompted again,
check `status`; an expired or invalid invitation requires a new Classic
Invitation from the PaperCut Hive administrator.

### Logs

Redacted service logs are stored under:

```text
%ProgramData%\Hive IPP Bridge\logs
```

Reading these service-owned logs may require an elevated Administrator window.
Do not copy credentials, invitation links, or captured PaperCut traffic into a
support report.
