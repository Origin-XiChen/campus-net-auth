' CampusNetAuthDaemon.vbs - silently launch the CampusNetAuth background daemon.
' Referenced by the HKCU Run registry key (installed via "CampusNetAuth.exe install").
' Pure ASCII on purpose. Window style 0 = fully hidden at logon, no console flash.
' Tolerant launcher: NEVER pops an error dialog (80070002). Failures are logged
' to autostart.log next to the script so the UI can surface them later.
On Error Resume Next
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
exePath = appDir & "\CampusNetAuth.exe"
If Not fso.FileExists(exePath) Then
    Set lf = fso.OpenTextFile(appDir & "\autostart.log", 8, True)
    If Err.Number = 0 Then
        lf.WriteLine Now & " [CampusNetAuth] daemon NOT started: exe missing at " & exePath
        lf.Close
    End If
    WScript.Quit 1
End If
Set sh = CreateObject("WScript.Shell")
Err.Clear
sh.Run """" & exePath & """ daemon", 0, False
If Err.Number <> 0 Then
    Set lf = fso.OpenTextFile(appDir & "\autostart.log", 8, True)
    If Err.Number = 0 Then
        lf.WriteLine Now & " [CampusNetAuth] daemon start FAILED: " & Err.Description
        lf.Close
    End If
End If
