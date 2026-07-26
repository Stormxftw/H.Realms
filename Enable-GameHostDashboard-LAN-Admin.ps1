#Requires -RunAsAdministrator
$ErrorActionPreference = "Stop"
$Port = 5057
$RuleName = "Hermes Game Host Console 5057 LAN"
Write-Host "Finding current WSL IP..."
$wslIpsRaw = & wsl.exe -e sh -lc "hostname -I"
$wslIp = ($wslIpsRaw -split "\s+" | Where-Object { $_ -match "^172\." } | Select-Object -First 1)
if (-not $wslIp) { throw "Could not determine WSL IP from: $wslIpsRaw" }
Write-Host "WSL IP: $wslIp"
Write-Host "Configuring Windows portproxy 0.0.0.0:$Port -> ${wslIp}:$Port ..."
& netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=$Port 2>$null | Out-Null
& netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=$Port connectaddress=$wslIp connectport=$Port | Out-Null
Write-Host "Configuring LAN-scoped firewall rule..."
Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private -RemoteAddress LocalSubnet | Out-Null
Write-Host ""; Write-Host "Portproxy:" -ForegroundColor Cyan
& netsh interface portproxy show v4tov4 | Select-String "$Port|Address|---"
Write-Host ""; Write-Host "Firewall:" -ForegroundColor Cyan
Get-NetFirewallRule -DisplayName $RuleName | Select-Object DisplayName,Enabled,Profile,Direction,Action | Format-Table -AutoSize
Write-Host ""; Write-Host "Open: http://10.0.0.2:$Port" -ForegroundColor Green
Write-Host "Run this again after WSL restarts if the LAN URL stops working."
Read-Host "Press Enter to exit"
