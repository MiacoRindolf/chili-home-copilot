' run-hidden.vbs - launch any command line with NO visible window.
'
' Why this exists: some CHILI scheduled tasks must run in the INTERACTIVE
' session (e.g. the Docker watchdog needs the user-session Docker named pipe
' and may launch Docker Desktop), so they cannot be moved to "run whether
' user is logged on or not" to hide them. Task Scheduler has no hidden-window
' option for interactive console apps, and powershell.exe -WindowStyle Hidden
' still flashes a console briefly. wscript.exe is a GUI-subsystem host, so a
' child started from here with window style 0 shows nothing at all.
'
' Usage (scheduled task action):
'   Execute:   wscript.exe
'   Arguments: "D:\dev\chili-home-copilot\scripts\run-hidden.vbs" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<script.ps1>" [args...]
'
' Waits for the child and propagates its exit code so Task Scheduler's
' "Last Run Result" stays meaningful.

If WScript.Arguments.Count = 0 Then
    WScript.Echo "usage: wscript run-hidden.vbs <command> [args...]"
    WScript.Quit 2
End If

Dim sh, cmd, i, a
Set sh = CreateObject("WScript.Shell")
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
    a = WScript.Arguments(i)
    If InStr(a, " ") > 0 Then a = Chr(34) & a & Chr(34)
    cmd = cmd & a & " "
Next

WScript.Quit sh.Run(Trim(cmd), 0, True)
