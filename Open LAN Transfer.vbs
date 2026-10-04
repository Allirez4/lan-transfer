Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
python = folder & "\backend\.venv\Scripts\pythonw.exe"
If Not files.FileExists(python) Then
    MsgBox "Run scripts\setup.ps1 first to install LAN Transfer.", 48, "LAN Transfer"
Else
    shell.Run Chr(34) & python & Chr(34) & " -m lan_transfer.desktop", 0, False
End If
