Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonwExe = scriptDir & "\venv\Scripts\pythonw.exe"
guiScript = scriptDir & "\gui_app.py"
q = Chr(34)

If Not fso.FileExists(pythonwExe) Then
    shell.Run q & scriptDir & "\run_gui.bat" & q, 1, False
Else
    shell.CurrentDirectory = scriptDir
    shell.Run q & pythonwExe & q & " " & q & guiScript & q, 0, False
End If