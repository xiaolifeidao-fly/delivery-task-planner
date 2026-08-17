param(
  [Parameter(Mandatory = $true)]
  [string]$RuntimeDir,
  [switch]$InstallTrust
)

$ErrorActionPreference = "Stop"

$tlsDirectory = Join-Path $RuntimeDir "tls"
$caCertificatePath = Join-Path $tlsDirectory "ca.pem"
$caKeyPath = Join-Path $tlsDirectory "ca-key.pem"
$bridgeCertificatePath = Join-Path $tlsDirectory "bridge-cert.pem"
$bridgeKeyPath = Join-Path $tlsDirectory "bridge-key.pem"
$caCommonName = "Universe Delivery Task Planner Local CA"

function Write-Pem {
  param(
    [string]$Path,
    [string]$Label,
    [byte[]]$Bytes
  )

  $base64 = [Convert]::ToBase64String($Bytes)
  $lines = [System.Collections.Generic.List[string]]::new()
  $lines.Add("-----BEGIN $Label-----")
  for ($offset = 0; $offset -lt $base64.Length; $offset += 64) {
    $count = [Math]::Min(64, $base64.Length - $offset)
    $lines.Add($base64.Substring($offset, $count))
  }
  $lines.Add("-----END $Label-----")
  [System.IO.File]::WriteAllLines($Path, [string[]]$lines, [System.Text.UTF8Encoding]::new($false))
}

function New-ExportableRsaKey {
  $parameters = [System.Security.Cryptography.CngKeyCreationParameters]::new()
  $parameters.ExportPolicy = [System.Security.Cryptography.CngExportPolicies]::AllowPlaintextExport
  $cngKey = [System.Security.Cryptography.CngKey]::Create([System.Security.Cryptography.CngAlgorithm]::Rsa, $null, $parameters)
  return [System.Security.Cryptography.RSACng]::new($cngKey)
}

function Export-Pkcs8PrivateKey {
  param([System.Security.Cryptography.RSA]$Key)

  try {
    return $Key.ExportPkcs8PrivateKey()
  } catch {
    if ($Key -is [System.Security.Cryptography.RSACng]) {
      return $Key.Key.Export([System.Security.Cryptography.CngKeyBlobFormat]::Pkcs8PrivateBlob)
    }
    throw
  }
}

New-Item -ItemType Directory -Path $tlsDirectory -Force | Out-Null
$certificateFiles = @($caCertificatePath, $caKeyPath, $bridgeCertificatePath, $bridgeKeyPath)
$allCertificateFilesExist = @($certificateFiles | Where-Object { -not (Test-Path -LiteralPath $_) }).Count -eq 0
if (-not $allCertificateFilesExist) {
  foreach ($path in $certificateFiles) {
    Remove-Item -LiteralPath $path -Force
  }

  $hashAlgorithm = [System.Security.Cryptography.HashAlgorithmName]::SHA256
  $signaturePadding = [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
  $now = [System.DateTimeOffset]::UtcNow
  $rootKey = New-ExportableRsaKey
  $rootRequest = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
    "CN=$caCommonName", $rootKey, $hashAlgorithm, $signaturePadding
  )
  $rootRequest.CertificateExtensions.Add([System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($true, $true, 0, $true))
  $rootUsage = [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::KeyCertSign -bor [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::CrlSign
  $rootRequest.CertificateExtensions.Add([System.Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new($rootUsage, $true))
  $rootRequest.CertificateExtensions.Add([System.Security.Cryptography.X509Certificates.X509SubjectKeyIdentifierExtension]::new($rootRequest.PublicKey, $false))
  $rootCertificate = $rootRequest.CreateSelfSigned($now.AddMinutes(-5), $now.AddYears(10))

  $bridgeKey = New-ExportableRsaKey
  $bridgeRequest = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
    "CN=127.0.0.1", $bridgeKey, $hashAlgorithm, $signaturePadding
  )
  $bridgeRequest.CertificateExtensions.Add([System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false, $false, 0, $true))
  $bridgeUsage = [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature -bor [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::KeyEncipherment
  $bridgeRequest.CertificateExtensions.Add([System.Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new($bridgeUsage, $true))
  $serverAuthenticationOids = [System.Security.Cryptography.OidCollection]::new()
  [void]$serverAuthenticationOids.Add([System.Security.Cryptography.Oid]::new("1.3.6.1.5.5.7.3.1"))
  $bridgeRequest.CertificateExtensions.Add([System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($serverAuthenticationOids, $false))
  $subjectAlternativeNames = [System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
  $subjectAlternativeNames.AddDnsName("localhost")
  $subjectAlternativeNames.AddIpAddress([System.Net.IPAddress]::Parse("127.0.0.1"))
  $subjectAlternativeNames.AddIpAddress([System.Net.IPAddress]::Parse("::1"))
  $bridgeRequest.CertificateExtensions.Add($subjectAlternativeNames.Build())
  $serial = [byte[]]::new(16)
  $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $random.GetBytes($serial)
  $bridgeCertificate = $bridgeRequest.Create($rootCertificate, $now.AddMinutes(-5), $now.AddDays(825), $serial).CopyWithPrivateKey($bridgeKey)

  Write-Pem -Path $caCertificatePath -Label "CERTIFICATE" -Bytes $rootCertificate.RawData
  Write-Pem -Path $caKeyPath -Label "PRIVATE KEY" -Bytes (Export-Pkcs8PrivateKey $rootKey)
  Write-Pem -Path $bridgeCertificatePath -Label "CERTIFICATE" -Bytes $bridgeCertificate.RawData
  Write-Pem -Path $bridgeKeyPath -Label "PRIVATE KEY" -Bytes (Export-Pkcs8PrivateKey $bridgeKey)
}

if ($InstallTrust) {
  $caCertificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($caCertificatePath)
  $trusted = Get-ChildItem -Path Cert:\CurrentUser\Root | Where-Object { $_.Thumbprint -eq $caCertificate.Thumbprint }
  if (-not $trusted) {
    Import-Certificate -FilePath $caCertificatePath -CertStoreLocation Cert:\CurrentUser\Root | Out-Null
  }
}

Write-Output "HTTPS bridge certificate: $bridgeCertificatePath"
Write-Output "HTTPS bridge private key: $bridgeKeyPath"
