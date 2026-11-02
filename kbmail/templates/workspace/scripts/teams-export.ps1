<#
.SYNOPSIS
  Export your Teams chats with Microsoft Graph PowerShell. You don't need your own Entra ID app.

.DESCRIPTION
  Signs in through Microsoft's own "Microsoft Graph Command Line Tools" app (the one behind the
  Microsoft.Graph PowerShell modules) with delegated Chat.Read, so you can only read chats you are
  a member of. The raw Graph responses are saved as JSON under -Out, unchanged. Then run
  `kbmail teams import`, or let `kbmail teams export --via powershell` run both steps.

  Delta: the time of the last successful run is kept in <Out>\since.txt. Pass -Days to override it.

  One-time setup, no admin rights needed:
      Install-Module Microsoft.Graph.Authentication -Scope CurrentUser
  Check that your tenant allows it before the first export:
      .\scripts\teams-export.ps1 -Test

.EXAMPLE
  .\scripts\teams-export.ps1 -Test
  .\scripts\teams-export.ps1 -Days 30
  .\scripts\teams-export.ps1 -DeviceCode     # sign in on another device / when no browser pops up
#>
[CmdletBinding()]
param(
    [int]$Days = 30,
    [string]$Out = "data/teams-graph",
    [switch]$DeviceCode,
    [switch]$Test
)
$ErrorActionPreference = "Stop"

# Application control (WDAC/AppLocker) puts PowerShell into Constrained Language Mode: only modules
# your IT allows can load, and this script can't call the .NET APIs it needs. That is a security
# control, not an error to work around: use another route instead.
if ($ExecutionContext.SessionState.LanguageMode -ne "FullLanguage") {
    Write-Host "PowerShell runs in $($ExecutionContext.SessionState.LanguageMode) mode on this computer (application control)."
    Write-Host "Microsoft Graph PowerShell can't load here, so this route is not available. Instead:"
    Write-Host "  - kbmail teams probe-outlook     (Teams chats via Outlook, if your profile exposes them)"
    Write-Host "  - Share to Outlook / kbmail teams import-text / meeting transcripts (see docs/teams.md)"
    Write-Host "  - or ask IT to allow-list the Microsoft-signed Microsoft.Graph.Authentication module"
    exit 5
}

if (-not (Get-Module -ListAvailable -Name Microsoft.Graph.Authentication)) {
    Write-Host "Microsoft Graph PowerShell is not installed. Install it for your user (no admin rights needed):"
    Write-Host "    Install-Module Microsoft.Graph.Authentication -Scope CurrentUser"
    exit 3
}
Import-Module Microsoft.Graph.Authentication

$connect = @{ Scopes = @("Chat.Read", "User.Read"); NoWelcome = $true }
if ($DeviceCode) { $connect.UseDeviceCode = $true }
try {
    Connect-MgGraph @connect
} catch {
    Write-Host "Sign-in failed: $($_.Exception.Message)"
    Write-Host "If it says admin approval/consent is required, your tenant blocks this app for Chat.Read."
    Write-Host "Use another route from docs/teams.md (Share to Outlook, pasted text, transcripts)."
    exit 4
}

function Get-RawPages([string]$Uri) {
    # Returns the raw JSON text of every page. Parsing happens only for paging.
    $pages = New-Object System.Collections.Generic.List[string]
    while ($Uri) {
        $raw = Invoke-MgGraphRequest -Method GET -Uri $Uri -OutputType Json
        $pages.Add($raw)
        $Uri = ($raw | ConvertFrom-Json).'@odata.nextLink'
    }
    return , $pages
}

if ($Test) {
    $me = Invoke-MgGraphRequest -Method GET -Uri "v1.0/me" -OutputType Json | ConvertFrom-Json
    $one = Invoke-MgGraphRequest -Method GET -Uri 'v1.0/me/chats?$top=1' -OutputType Json | ConvertFrom-Json
    Write-Host "OK: signed in as $($me.userPrincipalName); Chat.Read works ($(@($one.value).Count) chat returned)."
    exit 0
}

New-Item -ItemType Directory -Force -Path $Out | Out-Null
$sinceFile = Join-Path $Out "since.txt"
if (-not $PSBoundParameters.ContainsKey("Days") -and (Test-Path $sinceFile)) {
    $since = [datetime]::Parse((Get-Content $sinceFile -Raw).Trim(), $null,
        [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
} else {
    $since = (Get-Date).ToUniversalTime().AddDays(-$Days)
}
$startedAt = (Get-Date).ToUniversalTime()
$sinceIso = $since.ToString("yyyy-MM-ddTHH:mm:ssZ")
$stamp = $startedAt.ToString("yyyyMMdd-HHmmss")
$utf8 = New-Object System.Text.UTF8Encoding($false)

$chatPages = Get-RawPages 'v1.0/me/chats?$expand=members&$top=50'
[IO.File]::WriteAllText((Join-Path $Out "chats-$stamp.json"), "[" + ($chatPages -join ",") + "]", $utf8)

$n = 0
$total = 0
foreach ($page in $chatPages) {
    foreach ($chat in ($page | ConvertFrom-Json).value) {
        if ($chat.lastUpdatedDateTime -and ([datetime]$chat.lastUpdatedDateTime).ToUniversalTime() -le $since) {
            continue
        }
        $uri = "v1.0/chats/$($chat.id)/messages?`$top=50&`$orderby=lastModifiedDateTime desc" +
            "&`$filter=lastModifiedDateTime gt $sinceIso"
        $msgPages = Get-RawPages $uri
        $count = 0
        foreach ($p in $msgPages) { $count += @(($p | ConvertFrom-Json).value).Count }
        if ($count -eq 0) { continue }
        $n++
        $total += $count
        $json = '{"chatId":' + (ConvertTo-Json $chat.id) + ',"pages":[' + ($msgPages -join ",") + ']}'
        [IO.File]::WriteAllText((Join-Path $Out "messages-$stamp-$n.json"), $json, $utf8)
    }
}
# Overlap by an hour: kbmail de-duplicates messages by id.
[IO.File]::WriteAllText($sinceFile, $startedAt.AddHours(-1).ToString("o"), $utf8)
Write-Host "Exported $total message(s) from $n chat(s) changed since $sinceIso to $Out. Next: kbmail teams import"
