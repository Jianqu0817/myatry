$ErrorActionPreference='Stop'
[Console]::InputEncoding=[Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
try {
    $request=[Console]::In.ReadToEnd() | ConvertFrom-Json
    if ($request.action -eq 'launch') {
        $apps=@{notepad='notepad.exe';calculator='calc.exe'}
        if (-not $apps.ContainsKey([string]$request.application)) {throw 'application_not_allowed'}
        $proc=Start-Process -FilePath (Join-Path $env:SystemRoot ('System32/'+$apps[$request.application])) -PassThru
        @{status='launch_submitted';application=$request.application} | ConvertTo-Json -Compress
        exit 0
    }
    Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
    $root=[Windows.Automation.AutomationElement]::RootElement
    $windows=$root.FindAll([Windows.Automation.TreeScope]::Children,[Windows.Automation.Condition]::TrueCondition)
    $allowed=@('notepad','calculatorapp','calculator')
    $visible=@()
    foreach($entry in $windows) {
        try {
            $proc=Get-Process -Id $entry.Current.ProcessId -ErrorAction Stop
            $exe=$proc.Path
            if ($proc.ProcessName.ToLowerInvariant() -notin $allowed) {continue}
            if (-not ($exe.StartsWith($env:SystemRoot+'\',[StringComparison]::OrdinalIgnoreCase) -or $exe.StartsWith($env:ProgramFiles+'\WindowsApps\',[StringComparison]::OrdinalIgnoreCase))) {continue}
            $visible+=@{window=[string]$entry.Current.NativeWindowHandle;title=$entry.Current.Name;application=$proc.ProcessName;process_start=$proc.StartTime.ToUniversalTime().Ticks;node=$entry}
        }catch{continue}
    }
    if ($request.action -eq 'windows') {
        @{windows=@($visible | Select-Object window,title,application)} | ConvertTo-Json -Depth 5 -Compress
        exit 0
    }
    $selected=@($visible | Where-Object {$_.window -eq [string]$request.window})
    if ($selected.Count -ne 1) {throw 'window_not_available'}
    $selected=$selected[0]
    $nodes=$selected.node.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.Condition]::TrueCondition)
    if ($request.action -eq 'inspect') {
        $items=@()
        for($i=0;$i -lt [Math]::Min($nodes.Count,150);$i++) {
            $node=$nodes[$i]
            if ($node.Current.IsPassword) {continue}
            $value=$null;$invoke=$null
            $canValue=$node.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$value)
            $canInvoke=$node.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern,[ref]$invoke)
            $items+=@{name=$node.Current.Name;type=$node.Current.ControlType.ProgrammaticName;enabled=$node.Current.IsEnabled;runtime=($node.GetRuntimeId() -join ',');can_invoke=$canInvoke;can_set_value=($canValue -and -not $value.Current.IsReadOnly);value= $(if($canValue){$value.Current.Value.Substring(0,[Math]::Min(2000,$value.Current.Value.Length))}else{''})}
        }
        @{window=$selected.window;process_start=[string]$selected.process_start;elements=$items;truncated=($nodes.Count -gt 150)} | ConvertTo-Json -Depth 6 -Compress
        exit 0
    }
    if ([string]$selected.process_start -ne [string]$request.process_start) {throw 'window_changed'}
    $target=$null
    foreach($node in $nodes) {if (($node.GetRuntimeId() -join ',') -eq $request.runtime) {$target=$node;break}}
    if ($null -eq $target -or $target.Current.IsPassword -or -not $target.Current.IsEnabled) {throw 'element_unavailable'}
    if ($request.action -eq 'invoke') {
        $buttons=@('num0Button','num1Button','num2Button','num3Button','num4Button','num5Button','num6Button','num7Button','num8Button','num9Button','plusButton','minusButton','multiplyButton','divideButton','equalButton','decimalSeparatorButton','clearButton','clearEntryButton','backSpaceButton','negateButton')
        if ($selected.application -notin @('calculator','calculatorapp') -or $target.Current.AutomationId -notin $buttons) {throw 'control_not_allowed'}
        $target.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern).Invoke()
    }
    elseif ($request.action -eq 'set_value') {
        if (([string]$request.text).Length -gt 2000) {throw 'text_limit'}
        if ($selected.application -ne 'notepad' -or $target.Current.ControlType.ProgrammaticName -notin @('ControlType.Edit','ControlType.Document')) {throw 'control_not_allowed'}
        $target.GetCurrentPattern([Windows.Automation.ValuePattern]::Pattern).SetValue([string]$request.text)
    }else{throw 'unsupported_action'}
    @{status='action_submitted';verify_required=$true} | ConvertTo-Json -Compress
}catch{
    @{error='desktop_unavailable_or_outcome_unknown'} | ConvertTo-Json -Compress
    exit 1
}
