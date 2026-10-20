<#
.SYNOPSIS
Deploy webpagecheck to Azure Container Apps using Bicep and an image built in ACR.

.PARAMETER CustomEmailDomain
A custom domain already verified in the Email service (for example page.example.com) and linked to Communication Services. Pass it on every run; otherwise deployment restores the Azure-managed domain as the only linked domain.

.EXAMPLE
.\deploy.ps1 -ResourceGroup rg-webpagecheck
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [string] $Location = 'francecentral',
    [string] $NamePrefix = 'wpc',
    [string] $UiPassword,
    [bool] $DeployOpenAi = $true,
    [bool] $DeployEmail = $true,
    [string] $CustomEmailDomain = '',
    [string] $OpenAiLocation = 'swedencentral',
    [bool] $ChromiumSandbox = $true
)

$ErrorActionPreference = 'Stop'
# Azure CLI build logs can fail when the Windows console uses cp1252.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Invoke-Az {
    & az @args
    if ($LASTEXITCODE -ne 0) { throw "Azure CLI failed (exit code $LASTEXITCODE). Sensitive arguments are not displayed." }
}

function Format-Bool([bool] $value) { if ($value) { 'true' } else { 'false' } }

$template = Join-Path $PSScriptRoot 'infra\main.bicep'
$deployName = "webpagecheck-$(Get-Date -Format yyyyMMddHHmmss)"

$generatedPassword = $false
if (-not $UiPassword) {
    $chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789'
    $bytes = New-Object byte[] 24
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) }
    finally { $rng.Dispose() }
    $UiPassword = -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
    $generatedPassword = $true
}

$common = @(
    "namePrefix=$NamePrefix"
    "location=$Location"
    "deployOpenAi=$(Format-Bool $DeployOpenAi)"
    "openAiLocation=$OpenAiLocation"
    "deployEmail=$(Format-Bool $DeployEmail)"
    "customEmailDomain=$CustomEmailDomain"
    "chromiumSandbox=$(Format-Bool $ChromiumSandbox)"
)

$parameterDirectory = Join-Path ([IO.Path]::GetTempPath()) ("webpagecheck-" + [guid]::NewGuid().ToString('N'))
$parameterFile = Join-Path $parameterDirectory 'secrets.json'
try {
    [IO.Directory]::CreateDirectory($parameterDirectory) | Out-Null
    $acl = Get-Acl -LiteralPath $parameterDirectory
    $acl.SetAccessRuleProtection($true, $false)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
        $identity, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow'
    )
    $acl.SetAccessRule($rule)
    Set-Acl -LiteralPath $parameterDirectory -AclObject $acl
    $parameters = @{ uiPassword = @{ value = $UiPassword } } | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($parameterFile, $parameters, (New-Object Text.UTF8Encoding($false)))

Write-Host "1/4 Creating resource group $ResourceGroup"
Invoke-Az group create --name $ResourceGroup --location $Location --output none

Write-Host "2/4 Deploying infrastructure (ACR, identity, storage, environment, AI, email)"
Invoke-Az deployment group create --resource-group $ResourceGroup --name $deployName `
    --template-file $template --parameters "@$parameterFile" @common deployApp=false --output none

$outputs = az deployment group show --resource-group $ResourceGroup --name $deployName `
    --query properties.outputs -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Could not read deployment outputs' }

$tag = Get-Date -Format yyyyMMddHHmmss
$image = "$($outputs.acrLoginServer.value)/webpagecheck:$tag"

Write-Host "3/4 Building image in ACR $($outputs.acrName.value)"
Invoke-Az acr build --registry $outputs.acrName.value --image "webpagecheck:$tag" --no-logs $PSScriptRoot

Write-Host "4/4 Deploying the Container App"
$appDeployName = "$deployName-app"
Invoke-Az deployment group create --resource-group $ResourceGroup --name $appDeployName `
    --template-file $template --parameters "@$parameterFile" @common deployApp=true "image=$image" --output none

$final = az deployment group show --resource-group $ResourceGroup --name $appDeployName `
    --query properties.outputs -o json | ConvertFrom-Json

Write-Host ''
Write-Host "Web interface  : $($final.appUrl.value)"
Write-Host "Username       : (any value, for example admin)"
if ($generatedPassword) { Write-Host "Initial password: stored in the Container App secret 'ui-password' (not displayed)." }
if ($final.emailSender.value) { Write-Host "Email sender   : $($final.emailSender.value)" }
if ($final.openAiEndpoint.value) { Write-Host "Azure OpenAI endpoint: $($final.openAiEndpoint.value)" }
}
finally {
    if (Test-Path -LiteralPath $parameterFile) { Remove-Item -LiteralPath $parameterFile -Force }
    if (Test-Path -LiteralPath $parameterDirectory) { Remove-Item -LiteralPath $parameterDirectory -Force }
}
