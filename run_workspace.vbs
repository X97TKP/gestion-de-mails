Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "F:\mon-ia-web"
WshShell.Run "pythonw app.py", 0, False
