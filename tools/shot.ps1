Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$proc = Get-Process -Id 13764 -ErrorAction SilentlyContinue
if (-not $proc) { Write-Output "process gone"; exit 1 }
$rect = New-Object System.Drawing.Rectangle
Add-Type -Name U32 -Namespace W -MemberDefinition '[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool GetWindowRect(System.IntPtr h, out System.Drawing.Rectangle r); [System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);' -ReferencedAssemblies System.Drawing
$hwnd = $proc.MainWindowHandle
[W.U32]::SetForegroundWindow($hwnd) | Out-Null
Start-Sleep -Milliseconds 400
[W.U32]::GetWindowRect($hwnd, [ref]$rect) | Out-Null
$w = $rect.Width; $h = $rect.Height
if ($w -le 0 -or $h -le 0) {
    $b = $proc.MainWindowHandle
    Write-Output "no window rect, falling back to primary screen"
    $bnds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $w = $bnds.Width; $h = $bnds.Height; $rect = New-Object System.Drawing.Rectangle(0,0,$w,$h)
}
$bmp = New-Object System.Drawing.Bitmap($w, $h)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($rect.Location, [System.Drawing.Point]::Empty, (New-Object System.Drawing.Size($w,$h)))
$out = "D:\vibeflow_reverse\tools\vibeflow_window.png"
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output "saved $out ($w x $h)"
