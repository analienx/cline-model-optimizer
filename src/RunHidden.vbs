Option Explicit

If WScript.Arguments.Count < 1 Then WScript.Quit 87

Dim shell, command, i, value
command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " & Chr(34) & WScript.Arguments(0) & Chr(34)

For i = 1 To WScript.Arguments.Count - 1
    value = Replace(WScript.Arguments(i), Chr(34), Chr(34) & Chr(34))
    command = command & " " & Chr(34) & value & Chr(34)
Next

Set shell = CreateObject("WScript.Shell")
WScript.Quit shell.Run(command, 0, True)
