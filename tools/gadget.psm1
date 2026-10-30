# UI Automation driver for DSR Gadget (offline test sessions only, on a swapped-in test save).
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
if (-not ("GadgetNative" -as [type])) {
    Add-Type @"
using System; using System.Runtime.InteropServices;
public static class GadgetNative {
    [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
    public static void Click(IntPtr h) { SendMessage(h, 0x00F5, IntPtr.Zero, IntPtr.Zero); } // BM_CLICK
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr w, System.Text.StringBuilder l);
    [DllImport("user32.dll")] static extern IntPtr GetParent(IntPtr h);
    [DllImport("user32.dll")] static extern int GetDlgCtrlID(IntPtr h);
    public static string[] ComboItems(IntPtr h) {
        int n = (int)SendMessage(h, 0x0146, IntPtr.Zero, IntPtr.Zero); // CB_GETCOUNT
        string[] items = new string[Math.Max(0, n)];
        for (int i = 0; i < n; i++) {
            int len = (int)SendMessage(h, 0x0149, (IntPtr)i, IntPtr.Zero); // CB_GETLBTEXTLEN
            System.Text.StringBuilder sb = new System.Text.StringBuilder(len + 1);
            SendMessage(h, 0x0148, (IntPtr)i, sb); // CB_GETLBTEXT
            items[i] = sb.ToString();
        }
        return items;
    }
    public static int ComboSelected(IntPtr h) { return (int)SendMessage(h, 0x0147, IntPtr.Zero, IntPtr.Zero); } // CB_GETCURSEL
    // Select an index and send CBN_SELCHANGE to the parent so WinForms raises SelectedIndexChanged.
    public static void ComboSelect(IntPtr h, int i) {
        SendMessage(h, 0x014E, (IntPtr)i, IntPtr.Zero); // CB_SETCURSEL
        int id = GetDlgCtrlID(h);
        SendMessage(GetParent(h), 0x0111, (IntPtr)((1 << 16) | (id & 0xFFFF)), h); // WM_COMMAND, CBN_SELCHANGE
    }
}
"@
}

$script:GadgetExe = Join-Path (Split-Path -Parent $PSScriptRoot) "tools\bin\dsr-gadget\app\DSR Gadget 1.6\DSR-Gadget.exe"
$script:AE = [Windows.Automation.AutomationElement]

function Get-GadgetProcess { Get-Process -Name "DSR-Gadget" -ErrorAction SilentlyContinue | Select-Object -First 1 }

function Start-Gadget {
    if (-not (Get-GadgetProcess)) { Start-Process $script:GadgetExe | Out-Null; Start-Sleep -Seconds 4 }
    # Dismiss the startup warning dialog if present.
    $w = Get-GadgetWindow
    if ($w -and $w.Current.Name -eq "WARNING") {
        $ok = $w.FindFirst([Windows.Automation.TreeScope]::Descendants,
            (New-Object Windows.Automation.PropertyCondition($script:AE::NameProperty, "OK")))
        Invoke-GadgetElement $ok
        Start-Sleep -Seconds 3
    }
    Get-GadgetWindow
}

function Get-GadgetWindow {
    $p = Get-GadgetProcess
    if (-not $p) { return $null }
    $cond = New-Object Windows.Automation.PropertyCondition($script:AE::ProcessIdProperty, $p.Id)
    $script:AE::RootElement.FindFirst([Windows.Automation.TreeScope]::Children, $cond)
}

function Find-GadgetElement([string]$AutomationId, [string]$Name) {
    $w = Get-GadgetWindow
    $prop = if ($AutomationId) { $script:AE::AutomationIdProperty } else { $script:AE::NameProperty }
    $val = if ($AutomationId) { $AutomationId } else { $Name }
    $w.FindFirst([Windows.Automation.TreeScope]::Descendants, (New-Object Windows.Automation.PropertyCondition($prop, $val)))
}

function Invoke-GadgetElement($el) {
    if (-not $el) { throw "element not found" }
    $pat = $null
    if ($el.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern, [ref]$pat)) { $pat.Invoke(); return }
    if ($el.TryGetCurrentPattern([Windows.Automation.TogglePattern]::Pattern, [ref]$pat)) { $pat.Toggle(); return }
    if ($el.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pat)) { $pat.Select(); return }
    # Native Win32 buttons (e.g. MessageBox OK) expose no patterns; click them by handle.
    if ($el.Current.NativeWindowHandle -ne 0) { [GadgetNative]::Click([IntPtr]$el.Current.NativeWindowHandle); return }
    throw "element '$($el.Current.Name)' has no invoke/toggle/select pattern"
}

function Get-GadgetTree([int]$Max = 400) {
    $w = Get-GadgetWindow
    $w.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition) |
        Select-Object -First $Max | ForEach-Object {
            [pscustomobject]@{
                Type = $_.Current.ControlType.ProgrammaticName.Replace('ControlType.', '')
                Id = $_.Current.AutomationId; Name = $_.Current.Name; Enabled = $_.Current.IsEnabled
            }
        }
}

# The bonfire combo is the element whose current text is the selected bonfire; find it via the "Bonfire" label's
# sibling list by scanning combo-capable handles.
# UIA can't see controls of a minimized WinForms window, so handles are cached while it is visible.
$script:ComboHandle = [IntPtr]::Zero
$script:WarpHandle = [IntPtr]::Zero

function Show-Gadget {
    $w = Get-GadgetWindow
    if ($w) { [void][GadgetNative]::ShowWindow([IntPtr]$w.Current.NativeWindowHandle, 9); Start-Sleep -Milliseconds 500 }  # SW_RESTORE
}

function Get-GadgetBonfireCombo {
    if ($script:ComboHandle -ne [IntPtr]::Zero -and [GadgetNative]::ComboItems($script:ComboHandle).Count -gt 50) { return $script:ComboHandle }
    Show-Gadget
    $w = Get-GadgetWindow
    foreach ($el in $w.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)) {
        $h = [IntPtr]$el.Current.NativeWindowHandle
        if ($h -eq [IntPtr]::Zero) { continue }
        if ($el.Current.Name -eq "Warp" -and $script:WarpHandle -eq [IntPtr]::Zero) { $script:WarpHandle = $h }
        $items = [GadgetNative]::ComboItems($h)
        if ($items.Count -gt 50 -and ($items -join '|') -match 'Firelink') { $script:ComboHandle = $h }
    }
    if ($script:ComboHandle -eq [IntPtr]::Zero) { throw "bonfire combo not found" }
    return $script:ComboHandle
}

function Get-GadgetBonfires { [GadgetNative]::ComboItems((Get-GadgetBonfireCombo)) }

# Select a bonfire by exact name and press Warp. Caller waits for the load.
function Invoke-GadgetWarp([string]$Bonfire) {
    $combo = Get-GadgetBonfireCombo
    $items = [GadgetNative]::ComboItems($combo)
    $i = [array]::IndexOf($items, $Bonfire)
    if ($i -lt 0) { throw "unknown bonfire '$Bonfire'" }
    [GadgetNative]::ComboSelect($combo, $i)
    Start-Sleep -Milliseconds 400
    if ([GadgetNative]::ComboSelected($combo) -ne $i) { throw "bonfire selection did not stick" }
    if ($script:WarpHandle -eq [IntPtr]::Zero) { throw "warp button handle not cached" }
    [GadgetNative]::Click($script:WarpHandle)
    Hide-Gadget
}

# Minimize so the Gadget window never overlaps game captures (it keeps working minimized).
function Hide-Gadget {
    $w = Get-GadgetWindow
    if ($w) { [void][GadgetNative]::ShowWindow([IntPtr]$w.Current.NativeWindowHandle, 6) }  # SW_MINIMIZE
}

function Stop-Gadget { Get-GadgetProcess | Stop-Process -Force -ErrorAction SilentlyContinue }

Export-ModuleMember -Function Start-Gadget, Get-GadgetWindow, Find-GadgetElement, Invoke-GadgetElement, Get-GadgetTree,
    Stop-Gadget, Get-GadgetProcess, Get-GadgetBonfireCombo, Get-GadgetBonfires, Invoke-GadgetWarp, Hide-Gadget, Show-Gadget
