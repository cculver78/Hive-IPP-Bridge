#define MyAppName "Hive IPP Bridge"
#define MyAppVersion "0.1.0"

[Setup]
AppId={{C6AE0BE4-DB8A-4C7E-91BB-8D2268C6D9D8}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Edge Case Software
DefaultDirName={autopf}\Hive IPP Bridge
DefaultGroupName=Hive IPP Bridge
OutputDir=..\..\dist\installer
OutputBaseFilename=HiveIPPBridge-Setup
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Uninstallable=yes

[Files]
Source: "..\..\dist\HiveIPPBridge\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\..\packaging\windows\install-user.ps1"; DestDir: "{app}"; DestName: "Install-User.ps1"; Flags: ignoreversion
Source: "..\..\packaging\windows\uninstall-user.ps1"; DestDir: "{app}"; DestName: "Uninstall-User.ps1"; Flags: ignoreversion
Source: "..\..\packaging\windows\uninstall-system.ps1"; DestDir: "{app}"; DestName: "Uninstall-System.ps1"; Flags: ignoreversion

[Run]
Filename: "{app}\HiveIPPBridge.exe"; Parameters: "install"; Flags: runhidden waituntilterminated
Filename: "{app}\HiveIPPBridge.exe"; Parameters: "setup"; Description: "Configure Hive IPP Bridge now"; Flags: postinstall runasoriginaluser waituntilterminated skipifsilent

[UninstallRun]
Filename: "{app}\HiveIPPBridge.exe"; Parameters: "uninstall --machine"; RunOnceId: "HiveIPPBridgeUninstall"; Flags: runhidden waituntilterminated
