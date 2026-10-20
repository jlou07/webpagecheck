<#
.SYNOPSIS
Déploie webpagecheck sur Azure Container Apps (infra Bicep + image dans ACR).

.PARAMETER CustomEmailDomain
Domaine personnalisé déjà vérifié dans le service Email (par exemple page.example.com), relié à la ressource Communication Services. À passer à chaque exécution : sinon le déploiement remet le lien sur le seul domaine Azure.

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
# La CLI az plante sur les logs de build si la console Windows est en cp1252.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Invoke-Az {
    & az @args
    if ($LASTEXITCODE -ne 0) { throw "Azure CLI a échoué (code $LASTEXITCODE). Les arguments sensibles ne sont pas affichés." }
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

Write-Host "1/4 Création du groupe de ressources $ResourceGroup"
Invoke-Az group create --name $ResourceGroup --location $Location --output none

Write-Host "2/4 Déploiement de l'infrastructure (ACR, identité, stockage, environnement, IA, email)"
Invoke-Az deployment group create --resource-group $ResourceGroup --name $deployName `
    --template-file $template --parameters "@$parameterFile" @common deployApp=false --output none

$outputs = az deployment group show --resource-group $ResourceGroup --name $deployName `
    --query properties.outputs -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Lecture des sorties du déploiement impossible' }

$tag = Get-Date -Format yyyyMMddHHmmss
$image = "$($outputs.acrLoginServer.value)/webpagecheck:$tag"

Write-Host "3/4 Build de l'image dans ACR $($outputs.acrName.value)"
Invoke-Az acr build --registry $outputs.acrName.value --image "webpagecheck:$tag" --no-logs $PSScriptRoot

Write-Host "4/4 Déploiement de la Container App"
$appDeployName = "$deployName-app"
Invoke-Az deployment group create --resource-group $ResourceGroup --name $appDeployName `
    --template-file $template --parameters "@$parameterFile" @common deployApp=true "image=$image" --output none

$final = az deployment group show --resource-group $ResourceGroup --name $appDeployName `
    --query properties.outputs -o json | ConvertFrom-Json

Write-Host ''
Write-Host "Interface web  : $($final.appUrl.value)"
Write-Host "Utilisateur    : (n'importe lequel, par exemple admin)"
if ($generatedPassword) { Write-Host "Mot de passe initial : conservé dans le secret Container App 'ui-password' (non affiché)." }
if ($final.emailSender.value) { Write-Host "Expéditeur email : $($final.emailSender.value)" }
if ($final.openAiEndpoint.value) { Write-Host "Endpoint Azure OpenAI : $($final.openAiEndpoint.value)" }
}
finally {
    if (Test-Path -LiteralPath $parameterFile) { Remove-Item -LiteralPath $parameterFile -Force }
    if (Test-Path -LiteralPath $parameterDirectory) { Remove-Item -LiteralPath $parameterDirectory -Force }
}
