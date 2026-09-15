Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonwExe = scriptDir & "\venv\Scripts\pythonw.exe"
guiScript = scriptDir & "\gui_app.py"

If Not fso.FileExists(pythonwExe) Then
    shell.Run """" & scriptDir & "\run_gui.bat""", 1, False
Else
    shell.CurrentDirectory = scriptDir
    shell.Run """" & pythonwExe & """ """ & guiScript & """", 0, False
End If
