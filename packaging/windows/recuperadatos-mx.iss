; Instalador de RecuperaDatos-MX para Windows (Inno Setup 6)
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6E4D2B57-91A2-4C2B-9C1E-5D7A2F3B8E10}
AppName=RecuperaDatos-MX
AppVersion={#AppVersion}
AppVerName=RecuperaDatos-MX {#AppVersion}
AppPublisher=Santiago Miguel
AppPublisherURL=https://github.com/chago2850-blip/RecuperaDatos-MX
AppSupportURL=https://github.com/chago2850-blip/RecuperaDatos-MX/issues
DefaultDirName={autopf}\RecuperaDatos-MX
DefaultGroupName=RecuperaDatos-MX
LicenseFile={#LicenseFile}
OutputDir={#OutDir}
OutputBaseFilename=RecuperaDatos-MX-{#AppVersion}-windows-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\RecuperaDatos-MX.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible arm64
ArchitecturesInstallIn64BitMode=x64compatible arm64
PrivilegesRequired=admin

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\RecuperaDatos-MX"; Filename: "{app}\RecuperaDatos-MX.exe"
Name: "{group}\Desinstalar RecuperaDatos-MX"; Filename: "{uninstallexe}"
Name: "{autodesktop}\RecuperaDatos-MX"; Filename: "{app}\RecuperaDatos-MX.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\RecuperaDatos-MX.exe"; Description: "Abrir RecuperaDatos-MX"; Flags: nowait postinstall skipifsilent shellexec
