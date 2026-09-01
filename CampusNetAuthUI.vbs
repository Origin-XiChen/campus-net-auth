' CampusNetAuthUI.vbs - silently launch the CampusNetAuth settings window.
' Pure ASCII on purpose (avoid codepage issues). Window style 0 = hidden,
' so launching the console-subsystem exe shows no black window at all.
' Tolerant launcher: NEVER pops an error dialog (80070002). Failures are logged
' to autostart.log next to the script so the UI can surface them later.
On Error Resume Next
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
exePath = appDir & "\CampusNetAuth.exe"
If Not fso.FileExists(exePath) Then
    Set lf = fso.OpenTextFile(appDir & "\autostart.log", 8, True)
    If Err.Number = 0 Then
        lf.WriteLine Now & " [CampusNetAuth] UI NOT started: exe missing at " & exePath
        lf.Close
    End If
    WScript.Quit 1
End If
Set sh = CreateObject("WScript.Shell")
Err.Clear
sh.Run """" & exePath & """", 0, False
If Err.Number <> 0 Then
    Set lf = fso.OpenTextFile(appDir & "\autostart.log", 8, True)
    If Err.Number = 0 Then
        lf.WriteLine Now & " [CampusNetAuth] UI start FAILED: " & Err.Description
        lf.Close
    End If
End If
