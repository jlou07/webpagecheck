@description('Short resource prefix (3 to 8 lowercase letters and digits).')
@minLength(3)
@maxLength(8)
param namePrefix string = 'wpc'

param location string = resourceGroup().location

@description('Web interface password (Basic Auth).')
@secure()
param uiPassword string

@description('First pass: false (create infrastructure). Second pass: true (deploy the app from ACR).')
param deployApp bool = false

@description('Container image, required when deployApp = true.')
param image string = ''

@description('Chromium sandbox enabled by default. Use false only after explicitly accepting the risk on an incompatible host.')
param chromiumSandbox bool = true

param deployOpenAi bool = true
param openAiLocation string = 'swedencentral'
param openAiModelName string = 'gpt-4.1-mini'
param openAiModelVersion string = '2025-04-14'
param openAiDeploymentName string = 'gpt-4.1-mini'

param deployEmail bool = true

@description('Custom domain already verified in the Email service, for example page.example.com. Empty: Azure-managed domain only.')
param customEmailDomain string = ''

var suffix = uniqueString(resourceGroup().id)
var acrName = toLower('${namePrefix}acr${take(suffix, 12)}')
var storageName = toLower('${namePrefix}st${take(suffix, 12)}')
var openAiName = toLower('${namePrefix}-aoai-${take(suffix, 8)}')
var emailServiceName = toLower('${namePrefix}-email-${take(suffix, 8)}')
var acsName = toLower('${namePrefix}-acs-${take(suffix, 8)}')
var identityName = '${namePrefix}-id'
var logName = '${namePrefix}-log'
var envName = '${namePrefix}-env'
var appName = '${namePrefix}-web'
var shareName = 'webpagecheck-data'

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, acrPullRoleId)
  scope: acr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource openAi 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (deployOpenAi) {
  name: openAiName
  location: openAiLocation
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: openAiName
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

resource openAiModel 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = if (deployOpenAi) {
  parent: openAi
  name: openAiDeploymentName
  sku: {
    name: 'GlobalStandard'
    capacity: 30
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: openAiModelName
      version: openAiModelVersion
    }
  }
}

resource openAiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployOpenAi) {
  name: guid(openAi.id, identity.id, openAiUserRoleId)
  scope: openAi
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
  }
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileService
  name: shareName
  properties: {
    shareQuota: 1
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: env
  name: 'data'
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: shareName
      accessMode: 'ReadWrite'
    }
  }
}

resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = if (deployEmail) {
  name: emailServiceName
  location: 'global'
  properties: {
    dataLocation: 'Europe'
  }
}

resource emailDomain 'Microsoft.Communication/emailServices/domains@2023-04-01' = if (deployEmail) {
  parent: emailService
  name: 'AzureManagedDomain'
  location: 'global'
  properties: {
    domainManagement: 'AzureManaged'
  }
}

resource acs 'Microsoft.Communication/communicationServices@2023-04-01' = if (deployEmail) {
  name: acsName
  location: 'global'
  properties: {
    dataLocation: 'Europe'
    linkedDomains: concat(
      [
        emailDomain!.id
      ],
      empty(customEmailDomain) ? [] : [
        resourceId('Microsoft.Communication/emailServices/domains', emailServiceName, customEmailDomain)
      ]
    )
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = if (deployApp) {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acr.properties.loginServer
          identity: identity.id
        }
      ]
      secrets: concat(
        [
          {
            name: 'ui-password'
            value: uiPassword
          }
        ],
        deployEmail
          ? [
              {
                name: 'acs-connection-string'
                value: acs!.listKeys().primaryConnectionString
              }
            ]
          : []
      )
    }
    template: {
      containers: [
        {
          name: 'web'
          image: image
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(
            [
              {
                name: 'PORT'
                value: '8080'
              }
              {
                name: 'CHROMIUM_SANDBOX'
                value: string(chromiumSandbox)
              }
              {
                name: 'DATA_DIR'
                value: '/data'
              }
              {
                name: 'UI_PASSWORD'
                secretRef: 'ui-password'
              }
              {
                name: 'AZURE_CLIENT_ID'
                value: identity.properties.clientId
              }
            ],
            deployEmail
              ? [
                  {
                    name: 'ACS_CONNECTION_STRING'
                    secretRef: 'acs-connection-string'
                  }
                ]
              : [],
            deployOpenAi
              ? [
                  {
                    name: 'AZURE_OPENAI_ENDPOINT'
                    value: openAi!.properties.endpoint
                  }
                  {
                    name: 'AZURE_OPENAI_DEPLOYMENT'
                    value: openAiDeploymentName
                  }
                ]
              : []
          )
          volumeMounts: [
            {
              volumeName: 'data'
              mountPath: '/data'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      volumes: [
        {
          name: 'data'
          storageType: 'AzureFile'
          storageName: 'data'
        }
      ]
    }
  }
  dependsOn: [
    acrPull
    envStorage
  ]
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output appUrl string = deployApp ? 'https://${app!.properties.configuration.ingress.fqdn}' : ''
output openAiEndpoint string = deployOpenAi ? openAi!.properties.endpoint : ''
output emailSender string = deployEmail ? 'DoNotReply@${empty(customEmailDomain) ? emailDomain!.properties.mailFromSenderDomain : customEmailDomain}' : ''
