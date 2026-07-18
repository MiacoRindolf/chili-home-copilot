[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('ValidateOnly', 'NoOrderSmoke', 'ActivatePaper')]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [string]$PythonExecutable,

    [Parameter(Mandatory = $true)]
    [string]$CandidateRoot,

    [Parameter(Mandatory = $true)]
    [string]$ServiceScriptPath,

    [Parameter(Mandatory = $true)]
    [string]$Stage0ScriptPath,

    [Parameter(Mandatory = $true)]
    [string]$ManifestPath,

    [Parameter(Mandatory = $false)]
    [string]$NoOrderReceiptPath,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$ManifestSha256,

    [Parameter(Mandatory = $true)]
    [string]$AllowedReadRootsBase64
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Test-LocalPathEntryPresent {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath
    )

    # Test-Path follows a reparse point and can report a dangling link as
    # absent.  Activation authority paths are append-only, so inventory the
    # verified parent directory and treat any exact directory entry --
    # including a dangling reparse point -- as preexisting.
    $full = [IO.Path]::GetFullPath($LiteralPath)
    $parent = [IO.Path]::GetDirectoryName($full)
    if ([string]::IsNullOrWhiteSpace($parent)) {
        throw "The path has no inventoryable parent: $LiteralPath"
    }
    foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($parent)) {
        $entryFull = [IO.Path]::GetFullPath($entry)
        if ($entryFull.Equals($full, [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Resolve-StrictLocalPath {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][bool]$RequireFile
    )

    if ($LiteralPath -notmatch '^[A-Za-z]:[\\/]') {
        throw "Paths must be absolute local drive paths: $LiteralPath"
    }
    $item = Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
    $cursorPath = $item.FullName
    while (-not [string]::IsNullOrWhiteSpace($cursorPath)) {
        $cursor = Get-Item -LiteralPath $cursorPath -Force -ErrorAction Stop
        if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse-point paths are prohibited: $LiteralPath"
        }
        $parentPath = [IO.Path]::GetDirectoryName($cursorPath)
        if (
            [string]::IsNullOrWhiteSpace($parentPath) -or
            $parentPath.Equals($cursorPath, [StringComparison]::OrdinalIgnoreCase)
        ) {
            break
        }
        $cursorPath = $parentPath
    }
    if ($RequireFile -and $item.PSIsContainer) {
        throw "Expected a file: $LiteralPath"
    }
    if ((-not $RequireFile) -and (-not $item.PSIsContainer)) {
        throw "Expected a directory: $LiteralPath"
    }
    return $item.FullName
}

function Resolve-StrictLocalOutputPath {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath
    )

    if ($LiteralPath -notmatch '^[A-Za-z]:[\\/]') {
        throw "Output paths must be absolute: $LiteralPath"
    }
    if ($LiteralPath.StartsWith('\\') -or $LiteralPath.StartsWith('//')) {
        throw "UNC output paths are prohibited: $LiteralPath"
    }
    $leaf = [IO.Path]::GetFileName($LiteralPath)
    $parentLiteral = [IO.Path]::GetDirectoryName($LiteralPath)
    if ([string]::IsNullOrWhiteSpace($leaf) -or $leaf.IndexOf(':') -ge 0) {
        throw "The output path must name a regular local file: $LiteralPath"
    }
    if ([string]::IsNullOrWhiteSpace($parentLiteral)) {
        throw "The output path must have an existing local parent: $LiteralPath"
    }
    $parent = Resolve-StrictLocalPath -LiteralPath $parentLiteral -RequireFile $false
    $resolved = [IO.Path]::GetFullPath([IO.Path]::Combine($parent, $leaf))
    $resolvedParent = [IO.Path]::GetDirectoryName($resolved)
    if (-not $resolvedParent.Equals($parent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The output path escaped its verified parent: $LiteralPath"
    }
    if (Test-LocalPathEntryPresent -LiteralPath $resolved) {
        $existing = Resolve-StrictLocalPath -LiteralPath $resolved -RequireFile $true
        if (-not $existing.Equals($resolved, [StringComparison]::OrdinalIgnoreCase)) {
            throw "The output path changed while resolving: $LiteralPath"
        }
    }
    return $resolved
}

function ConvertTo-CanonicalLocalPath {
    param(
        [Parameter(Mandatory = $true)][string]$ResolvedPath
    )

    if ($ResolvedPath -notmatch '^[A-Za-z]:[\\/]') {
        throw "Canonical paths must be absolute local drive paths: $ResolvedPath"
    }
    $full = [IO.Path]::GetFullPath($ResolvedPath).Replace('/', '\')
    $root = [IO.Path]::GetPathRoot($full).Replace('/', '\')
    if ($full.Length -gt $root.Length) {
        $full = $full.TrimEnd('\')
    }
    return $full.ToLowerInvariant()
}

function Get-Sha256Text {
    param(
        [Parameter(Mandatory = $true)][string]$Value
    )

    $utf8 = [Text.UTF8Encoding]::new($false)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $utf8.GetBytes($Value)
        return -join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })
    }
    finally {
        $sha.Dispose()
    }
}

function Assert-ExactObjectProperties {
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($null -eq $Value) {
        throw "$Label is missing."
    }
    $actual = @($Value.PSObject.Properties.Name | Sort-Object)
    $wanted = @($Expected | Sort-Object)
    if (
        $actual.Count -ne $wanted.Count -or
        @(Compare-Object -ReferenceObject $wanted -DifferenceObject $actual -CaseSensitive).Count -ne 0
    ) {
        throw "$Label has an unexpected schema."
    }
}

function ConvertTo-CanonicalProjectionJson {
    param(
        [Parameter(Mandatory = $true)][object]$Projection
    )

    $projectionKeys = @(
        'allowed_read_roots',
        'candidate_root',
        'foreground',
        'host_ready_receipt_base',
        'launcher_path',
        'launcher_sha256',
        'launcher_source_path',
        'launcher_source_sha256',
        'manifest_path',
        'manifest_schema_version',
        'manifest_sha256',
        'mode',
        'no_order_receipt_output_path',
        'no_order_receipt_output_policy',
        'python_executable_path',
        'python_executable_sha256',
        'python_dependency_root',
        'python_dependency_root_identity_sha256',
        'python_import_root',
        'schema_version',
        'service_arguments',
        'service_mode',
        'service_path',
        'service_sha256',
        'service_source_path',
        'service_source_sha256',
        'service_staged_path',
        'stage0_path',
        'stage0_sha256',
        'stage0_source_path',
        'stage0_source_sha256',
        'singleton_name',
        'working_directory'
    )
    Assert-ExactObjectProperties -Value $Projection -Expected $projectionKeys -Label 'launcher projection'
    $ordered = [ordered]@{
        allowed_read_roots = @($Projection.allowed_read_roots)
        candidate_root = $Projection.candidate_root
        foreground = $Projection.foreground
        host_ready_receipt_base = $Projection.host_ready_receipt_base
        launcher_path = $Projection.launcher_path
        launcher_sha256 = $Projection.launcher_sha256
        launcher_source_path = $Projection.launcher_source_path
        launcher_source_sha256 = $Projection.launcher_source_sha256
        manifest_path = $Projection.manifest_path
        manifest_schema_version = $Projection.manifest_schema_version
        manifest_sha256 = $Projection.manifest_sha256
        mode = $Projection.mode
        no_order_receipt_output_path = $Projection.no_order_receipt_output_path
        no_order_receipt_output_policy = $Projection.no_order_receipt_output_policy
        python_dependency_root = $Projection.python_dependency_root
        python_dependency_root_identity_sha256 = $Projection.python_dependency_root_identity_sha256
        python_executable_path = $Projection.python_executable_path
        python_executable_sha256 = $Projection.python_executable_sha256
        python_import_root = $Projection.python_import_root
        schema_version = $Projection.schema_version
        service_arguments = @($Projection.service_arguments)
        service_mode = $Projection.service_mode
        service_path = $Projection.service_path
        service_sha256 = $Projection.service_sha256
        service_source_path = $Projection.service_source_path
        service_source_sha256 = $Projection.service_source_sha256
        service_staged_path = $Projection.service_staged_path
        singleton_name = $Projection.singleton_name
        stage0_path = $Projection.stage0_path
        stage0_sha256 = $Projection.stage0_sha256
        stage0_source_path = $Projection.stage0_source_path
        stage0_source_sha256 = $Projection.stage0_source_sha256
        working_directory = $Projection.working_directory
    }
    return ($ordered | ConvertTo-Json -Compress -Depth 5)
}

function New-LauncherInvocationProjection {
    param(
        [Parameter(Mandatory = $true)][string]$SelectedMode,
        [Parameter(Mandatory = $true)][string]$SelectedServiceMode,
        [Parameter(Mandatory = $true)][string]$ManifestSchemaVersion,
        [Parameter(Mandatory = $true)][string]$CanonicalCandidate,
        [Parameter(Mandatory = $true)][string]$CanonicalPython,
        [Parameter(Mandatory = $true)][string]$PythonSha256,
        [Parameter(Mandatory = $true)][string]$CanonicalDependencyRoot,
        [Parameter(Mandatory = $true)][string]$DependencyRootIdentitySha256,
        [Parameter(Mandatory = $true)][string[]]$CanonicalReadRoots,
        [Parameter(Mandatory = $true)][string]$CanonicalLauncherSource,
        [Parameter(Mandatory = $true)][string]$CanonicalLauncher,
        [Parameter(Mandatory = $true)][string]$LauncherSha256,
        [Parameter(Mandatory = $true)][string]$CanonicalStage0Source,
        [Parameter(Mandatory = $true)][string]$CanonicalStage0,
        [Parameter(Mandatory = $true)][string]$Stage0Sha256,
        [Parameter(Mandatory = $true)][string]$CanonicalServiceSource,
        [Parameter(Mandatory = $true)][string]$CanonicalService,
        [Parameter(Mandatory = $true)][string]$ServiceSha256,
        [Parameter(Mandatory = $true)][string]$ReceiptPolicy,
        [AllowNull()][string]$CanonicalReceiptOutput,
        [AllowNull()][string]$CanonicalHostReadyReceipt
    )

    $manifestPathToken = '@verified:content-addressed-manifest-path'
    $manifestShaToken = '@verified:manifest-file-sha256'
    $projectedArguments = @(
        '-I',
        '-S',
        '-B',
        $CanonicalStage0,
        '--manifest', $manifestPathToken,
        '--manifest-sha256', $manifestShaToken,
        '--candidate-root', $CanonicalCandidate,
        '--target-role', 'activation_service',
        '--target', $CanonicalService,
        '--target-sha256', $ServiceSha256,
        '--',
        '--mode', $SelectedServiceMode,
        '--manifest', $manifestPathToken,
        '--manifest-sha256', $manifestShaToken,
        '--candidate-root', $CanonicalCandidate,
        '--launcher-path', $CanonicalLauncher,
        '--launcher-sha256', $LauncherSha256
    )
    foreach ($root in $CanonicalReadRoots) {
        $projectedArguments += @('--allow-read-root', $root)
    }
    if ($ReceiptPolicy -eq 'required') {
        if ([string]::IsNullOrWhiteSpace($CanonicalReceiptOutput)) {
            throw 'The selected launcher projection requires a receipt output.'
        }
        $projectedArguments += @('--no-order-receipt-output', $CanonicalReceiptOutput)
        $receiptOutputValue = $CanonicalReceiptOutput
    }
    else {
        if (
            $ReceiptPolicy -ne 'forbidden' -or
            -not [string]::IsNullOrEmpty($CanonicalReceiptOutput)
        ) {
            throw 'The selected launcher projection forbids a receipt output.'
        }
        $receiptOutputValue = $null
    }
    if ($SelectedMode -eq 'ActivatePaper') {
        if ([string]::IsNullOrWhiteSpace($CanonicalHostReadyReceipt)) {
            throw 'The ActivatePaper projection requires a host-ready path.'
        }
        $projectedArguments += @(
            '--host-ready-receipt', $CanonicalHostReadyReceipt
        )
        $hostReadyValue = $CanonicalHostReadyReceipt
    }
    elseif (-not [string]::IsNullOrEmpty($CanonicalHostReadyReceipt)) {
        throw 'Only ActivatePaper may project a host-ready path.'
    }
    else {
        $hostReadyValue = $null
    }

    return [pscustomobject][ordered]@{
        allowed_read_roots = @($CanonicalReadRoots)
        candidate_root = $CanonicalCandidate
        foreground = $true
        host_ready_receipt_base = $hostReadyValue
        launcher_source_path = $CanonicalLauncherSource
        launcher_source_sha256 = $LauncherSha256
        launcher_path = $CanonicalLauncher
        launcher_sha256 = $LauncherSha256
        manifest_path = $manifestPathToken
        manifest_schema_version = $ManifestSchemaVersion
        manifest_sha256 = $manifestShaToken
        mode = $SelectedMode
        no_order_receipt_output_path = $receiptOutputValue
        no_order_receipt_output_policy = $ReceiptPolicy
        python_executable_path = $CanonicalPython
        python_executable_sha256 = $PythonSha256
        python_dependency_root = $CanonicalDependencyRoot
        python_dependency_root_identity_sha256 = $DependencyRootIdentitySha256
        python_import_root = $CanonicalCandidate
        schema_version = 'chili.captured-paper-launcher-invocation-projection.v1'
        service_arguments = $projectedArguments
        service_mode = $SelectedServiceMode
        service_source_path = $CanonicalServiceSource
        service_source_sha256 = $ServiceSha256
        service_staged_path = $CanonicalService
        service_path = $CanonicalService
        service_sha256 = $ServiceSha256
        stage0_source_path = $CanonicalStage0Source
        stage0_source_sha256 = $Stage0Sha256
        stage0_path = $CanonicalStage0
        stage0_sha256 = $Stage0Sha256
        singleton_name = 'Global\CHILI-Captured-Alpaca-PAPER-SINGLETON'
        working_directory = $CanonicalCandidate
    }
}

function Test-CanonicalPathInsideRoot {
    param(
        [Parameter(Mandatory = $true)][string]$CanonicalPath,
        [Parameter(Mandatory = $true)][string]$CanonicalRoot
    )

    if ($CanonicalPath.Equals($CanonicalRoot, [StringComparison]::Ordinal)) {
        return $true
    }
    $prefix = $CanonicalRoot.TrimEnd('\') + '\'
    return $CanonicalPath.StartsWith($prefix, [StringComparison]::Ordinal)
}

$launcherPath = Resolve-StrictLocalPath -LiteralPath $PSCommandPath -RequireFile $true
$candidate = Resolve-StrictLocalPath -LiteralPath $CandidateRoot -RequireFile $false
$python = Resolve-StrictLocalPath -LiteralPath $PythonExecutable -RequireFile $true
$manifest = Resolve-StrictLocalPath -LiteralPath $ManifestPath -RequireFile $true
$readRootBase64 = $AllowedReadRootsBase64.Trim()
try {
    $readRootJsonBytes = [Convert]::FromBase64String($readRootBase64)
}
catch {
    throw 'AllowedReadRootsBase64 must be canonical base64.'
}
if ([Convert]::ToBase64String($readRootJsonBytes) -cne $readRootBase64) {
    throw 'AllowedReadRootsBase64 must be canonical base64.'
}
$readRootJsonText = [Text.UTF8Encoding]::new($false, $true).GetString(
    $readRootJsonBytes
)
if (-not ($readRootJsonText.StartsWith('[') -and $readRootJsonText.EndsWith(']'))) {
    throw 'AllowedReadRootsBase64 must decode to one JSON array.'
}
try {
    $parsedReadRoots = $readRootJsonText | ConvertFrom-Json -ErrorAction Stop
    $decodedReadRoots = @($parsedReadRoots)
}
catch {
    throw 'AllowedReadRootsBase64 must decode to valid JSON.'
}
if (
    $decodedReadRoots.Count -eq 0 -or
    @($decodedReadRoots | Where-Object {
        $_ -isnot [string] -or [string]::IsNullOrWhiteSpace($_)
    }).Count -ne 0
) {
    throw 'AllowedReadRootsBase64 must contain only nonempty path strings.'
}
$readRootByCanonical = @{}
foreach ($requestedRoot in $decodedReadRoots) {
    $resolvedRoot = Resolve-StrictLocalPath -LiteralPath $requestedRoot -RequireFile $false
    $canonicalRoot = ConvertTo-CanonicalLocalPath -ResolvedPath $resolvedRoot
    if ($readRootByCanonical.ContainsKey($canonicalRoot)) {
        throw 'AllowedReadRoot contains the same canonical root more than once.'
    }
    $readRootByCanonical[$canonicalRoot] = $resolvedRoot
}
if ($readRootByCanonical.Count -eq 0) {
    throw 'At least one AllowedReadRoot is required.'
}
$canonicalReadRoots = [string[]]@($readRootByCanonical.Keys)
[Array]::Sort($canonicalReadRoots, [StringComparer]::Ordinal)
$readRoots = @($canonicalReadRoots | ForEach-Object { $readRootByCanonical[$_] })
$service = Resolve-StrictLocalPath -LiteralPath $ServiceScriptPath -RequireFile $true
$stage0 = Resolve-StrictLocalPath -LiteralPath $Stage0ScriptPath -RequireFile $true

$noOrderReceipt = $null
if ($Mode -eq 'NoOrderSmoke') {
    if ([string]::IsNullOrWhiteSpace($NoOrderReceiptPath)) {
        throw 'NoOrderReceiptPath is required for NoOrderSmoke.'
    }
    $noOrderReceipt = Resolve-StrictLocalOutputPath -LiteralPath $NoOrderReceiptPath
}
elseif (-not [string]::IsNullOrWhiteSpace($NoOrderReceiptPath)) {
    throw 'NoOrderReceiptPath is accepted only for NoOrderSmoke.'
}

$actualManifestSha = (
    Get-FileHash -LiteralPath $manifest -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($actualManifestSha -ne $ManifestSha256.ToLowerInvariant()) {
    throw 'The PAPER activation manifest hash does not match.'
}
$launcherSha = (Get-FileHash -LiteralPath $launcherPath -Algorithm SHA256).Hash.ToLowerInvariant()
$serviceSha = (Get-FileHash -LiteralPath $service -Algorithm SHA256).Hash.ToLowerInvariant()
$stage0Sha = (Get-FileHash -LiteralPath $stage0 -Algorithm SHA256).Hash.ToLowerInvariant()
$pythonSha = (Get-FileHash -LiteralPath $python -Algorithm SHA256).Hash.ToLowerInvariant()
$canonicalCandidate = ConvertTo-CanonicalLocalPath -ResolvedPath $candidate
$canonicalPython = ConvertTo-CanonicalLocalPath -ResolvedPath $python
$canonicalManifest = ConvertTo-CanonicalLocalPath -ResolvedPath $manifest
$canonicalLauncher = ConvertTo-CanonicalLocalPath -ResolvedPath $launcherPath
$canonicalService = ConvertTo-CanonicalLocalPath -ResolvedPath $service
$canonicalStage0 = ConvertTo-CanonicalLocalPath -ResolvedPath $stage0
$canonicalNoOrderReceipt = if ($null -ne $noOrderReceipt) {
    ConvertTo-CanonicalLocalPath -ResolvedPath $noOrderReceipt
} else {
    $null
}

if (
    -not [IO.Path]::GetFileName($launcherPath).Equals(
        ($launcherSha + '.ps1'), [StringComparison]::Ordinal
    ) -or
    -not [IO.Path]::GetFileName(
        [IO.Path]::GetDirectoryName($launcherPath)
    ).Equals($launcherSha, [StringComparison]::Ordinal)
) {
    throw 'The PAPER launcher is not executing from immutable SHA-addressed bytes.'
}
if (
    -not [IO.Path]::GetFileName($service).Equals(
        ($serviceSha + '.py'), [StringComparison]::Ordinal
    ) -or
    -not [IO.Path]::GetFileName(
        [IO.Path]::GetDirectoryName($service)
    ).Equals($serviceSha, [StringComparison]::Ordinal)
) {
    throw 'The PAPER service is not executing from immutable SHA-addressed bytes.'
}
if (
    -not [IO.Path]::GetFileName($stage0).Equals(
        ($stage0Sha + '.py'), [StringComparison]::Ordinal
    ) -or
    -not [IO.Path]::GetFileName(
        [IO.Path]::GetDirectoryName($stage0)
    ).Equals($stage0Sha, [StringComparison]::Ordinal)
) {
    throw 'The PAPER stage-0 is not executing from immutable SHA-addressed bytes.'
}

foreach ($requiredPath in @(
    $canonicalCandidate,
    $canonicalPython,
    $canonicalManifest,
    $canonicalLauncher,
    $canonicalService,
    $canonicalStage0
)) {
    if (-not @($canonicalReadRoots | Where-Object {
        Test-CanonicalPathInsideRoot -CanonicalPath $requiredPath -CanonicalRoot $_
    }).Count) {
        throw 'A launcher input escaped the exact AllowedReadRoot contract.'
    }
}
if (
    $null -ne $canonicalNoOrderReceipt -and
    -not @($canonicalReadRoots | Where-Object {
        Test-CanonicalPathInsideRoot -CanonicalPath $canonicalNoOrderReceipt -CanonicalRoot $_
    }).Count
) {
    throw 'The no-order receipt output escaped the exact AllowedReadRoot contract.'
}

# Remove cash-broker authority from this launcher process before constructing
# the child.  The Python pre-import loader repeats this check and imports only
# the content-addressed PAPER allowlist.
$forbiddenExact = @(
    'CHILI_ALPACA_LIVE_API_KEY',
    'CHILI_ALPACA_LIVE_API_SECRET',
    'ALPACA_API_KEY',
    'ALPACA_API_SECRET',
    'APCA_API_KEY_ID',
    'APCA_API_SECRET_KEY',
    'APCA_API_BASE_URL',
    'COINBASE_API_KEY',
    'COINBASE_API_SECRET',
    'COINBASE_PRIVATE_KEY',
    'ROBINHOOD_USERNAME',
    'ROBINHOOD_PASSWORD',
    'ROBINHOOD_MFA_CODE'
)
$forbiddenPrefixes = @(
    'CHILI_ALPACA_LIVE_', 'ALPACA_LIVE_',
    'CHILI_ROBINHOOD_', 'ROBINHOOD_', 'ROBIN_STOCKS_',
    'CHILI_COINBASE_', 'COINBASE_',
    'CHILI_KRAKEN_', 'KRAKEN_',
    'CHILI_BINANCE_', 'BINANCE_',
    'CHILI_OANDA_', 'OANDA_',
    'CHILI_TRADIER_', 'TRADIER_',
    'CHILI_HYPERLIQUID_', 'HYPERLIQUID_',
    'CHILI_DYDX_', 'DYDX_'
)
Get-ChildItem Env: | ForEach-Object {
    $name = $_.Name.ToUpperInvariant()
    $remove = $forbiddenExact -contains $name
    if (-not $remove) {
        foreach ($prefix in $forbiddenPrefixes) {
            if ($name.StartsWith($prefix, [StringComparison]::Ordinal)) {
                $remove = $true
                break
            }
        }
    }
    if ($remove) {
        Remove-Item -LiteralPath ("Env:" + $_.Name) -ErrorAction Stop
    }
}

$serviceMode = switch ($Mode) {
    'ValidateOnly' { 'validate-only' }
    'NoOrderSmoke' { 'no-order-smoke' }
    'ActivatePaper' { 'activate-paper' }
    default { throw 'Unsupported PAPER service mode.' }
}

$expectedManifestSchema = switch ($Mode) {
    'NoOrderSmoke' { 'chili.captured-paper-preactivation.v2' }
    'ValidateOnly' { 'chili.captured-paper-activation.v3' }
    'ActivatePaper' { 'chili.captured-paper-activation.v3' }
    default { throw 'Unsupported PAPER manifest mode.' }
}
$receiptPolicy = if ($Mode -eq 'NoOrderSmoke') { 'required' } else { 'forbidden' }

# The manifest path and its outer SHA are deliberately verified before being
# represented by fixed tokens in the invocation projection.  A manifest cannot
# contain a literal hash commitment to a projection that itself contains that
# same manifest's outer hash; the fixed tokens avoid that impossible cycle
# without weakening either literal file check.
$expectedManifestLeaf = $actualManifestSha + '.json'
$manifestParentLeaf = [IO.Path]::GetFileName([IO.Path]::GetDirectoryName($manifest))
if (
    -not [IO.Path]::GetFileName($manifest).Equals(
        $expectedManifestLeaf, [StringComparison]::OrdinalIgnoreCase
    ) -or
    -not $manifestParentLeaf.Equals(
        $actualManifestSha.Substring(0, 2), [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw 'The PAPER activation manifest is not at its content-addressed path.'
}
try {
    $manifestDocument = [IO.File]::ReadAllText($manifest) | ConvertFrom-Json -ErrorAction Stop
}
catch {
    throw 'The PAPER activation manifest is not valid JSON.'
}
if ($manifestDocument.schema_version -cne $expectedManifestSchema) {
    throw 'The PAPER activation manifest schema does not match the selected mode.'
}
$cutoverKeys = @(
    'activation_artifact_root',
    'candidate_root',
    'host_ready_receipt_base',
    'launcher_source_path',
    'launcher_source_sha256',
    'launcher_path',
    'launcher_sha256',
    'launcher_arguments_path',
    'launcher_arguments_sha256',
    'python_executable_path',
    'python_executable_sha256',
    'python_dependency_root',
    'python_dependency_root_identity_sha256',
    'python_import_root',
    'scheduled_tasks',
    'service_source_path',
    'service_source_sha256',
    'service_path',
    'service_sha256',
    'stage0_source_path',
    'stage0_source_sha256',
    'stage0_path',
    'stage0_sha256',
    'singleton_policy',
    'rollback_required'
)
Assert-ExactObjectProperties -Value $manifestDocument.cutover -Expected $cutoverKeys -Label 'manifest cutover'
$launcherSource = Resolve-StrictLocalPath `
    -LiteralPath ([string]$manifestDocument.cutover.launcher_source_path) `
    -RequireFile $true
$serviceSource = Resolve-StrictLocalPath `
    -LiteralPath ([string]$manifestDocument.cutover.service_source_path) `
    -RequireFile $true
$stage0Source = Resolve-StrictLocalPath `
    -LiteralPath ([string]$manifestDocument.cutover.stage0_source_path) `
    -RequireFile $true
$dependencyRoot = Resolve-StrictLocalPath `
    -LiteralPath ([string]$manifestDocument.cutover.python_dependency_root) `
    -RequireFile $false
$activationArtifactRoot = Resolve-StrictLocalPath `
    -LiteralPath ([string]$manifestDocument.cutover.activation_artifact_root) `
    -RequireFile $false
$canonicalLauncherSource = ConvertTo-CanonicalLocalPath -ResolvedPath $launcherSource
$canonicalServiceSource = ConvertTo-CanonicalLocalPath -ResolvedPath $serviceSource
$canonicalStage0Source = ConvertTo-CanonicalLocalPath -ResolvedPath $stage0Source
$canonicalDependencyRoot = ConvertTo-CanonicalLocalPath -ResolvedPath $dependencyRoot
$canonicalActivationArtifactRoot = ConvertTo-CanonicalLocalPath `
    -ResolvedPath $activationArtifactRoot
$manifestHostReadyReceipt = Resolve-StrictLocalOutputPath `
    -LiteralPath ([string]$manifestDocument.cutover.host_ready_receipt_base)
$canonicalManifestHostReadyReceipt = ConvertTo-CanonicalLocalPath `
    -ResolvedPath $manifestHostReadyReceipt
$declaredLauncherSourceSha = (
    [string]$manifestDocument.cutover.launcher_source_sha256
).ToLowerInvariant()
$declaredServiceSourceSha = (
    [string]$manifestDocument.cutover.service_source_sha256
).ToLowerInvariant()
$declaredStage0SourceSha = (
    [string]$manifestDocument.cutover.stage0_source_sha256
).ToLowerInvariant()
$dependencyRootIdentitySha = (
    [string]$manifestDocument.cutover.python_dependency_root_identity_sha256
).ToLowerInvariant()
$activationGeneration = [string]$manifestDocument.activation_generation
$expectedGenerationRoot = [IO.Path]::GetFullPath(
    [IO.Path]::Combine($activationArtifactRoot, $activationGeneration)
)
$expectedLauncherPath = [IO.Path]::GetFullPath(
    [IO.Path]::Combine(
        $expectedGenerationRoot,
        $launcherSha,
        ($launcherSha + '.ps1')
    )
)
$expectedServicePath = [IO.Path]::GetFullPath(
    [IO.Path]::Combine(
        $expectedGenerationRoot,
        $serviceSha,
        ($serviceSha + '.py')
    )
)
$expectedStage0Path = [IO.Path]::GetFullPath(
    [IO.Path]::Combine(
        $expectedGenerationRoot,
        $stage0Sha,
        ($stage0Sha + '.py')
    )
)
$expectedHostReadyPath = [IO.Path]::GetFullPath(
    [IO.Path]::Combine($expectedGenerationRoot, 'handshake', 'host-ready.json')
)
if (
    $activationGeneration -notmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$' -or
    (ConvertTo-CanonicalLocalPath -ResolvedPath ([string]$manifestDocument.cutover.candidate_root)) -cne $canonicalCandidate -or
    (ConvertTo-CanonicalLocalPath -ResolvedPath ([string]$manifestDocument.cutover.python_import_root)) -cne $canonicalCandidate -or
    (ConvertTo-CanonicalLocalPath -ResolvedPath ([string]$manifestDocument.cutover.python_executable_path)) -cne $canonicalPython -or
    ([string]$manifestDocument.cutover.python_executable_sha256).ToLowerInvariant() -cne $pythonSha -or
    $dependencyRootIdentitySha -notmatch '^[0-9a-f]{64}$' -or
    (ConvertTo-CanonicalLocalPath -ResolvedPath ([string]$manifestDocument.cutover.launcher_path)) -cne $canonicalLauncher -or
    ([string]$manifestDocument.cutover.launcher_sha256).ToLowerInvariant() -cne $launcherSha -or
    (ConvertTo-CanonicalLocalPath -ResolvedPath ([string]$manifestDocument.cutover.service_path)) -cne $canonicalService -or
    ([string]$manifestDocument.cutover.service_sha256).ToLowerInvariant() -cne $serviceSha -or
    (ConvertTo-CanonicalLocalPath -ResolvedPath ([string]$manifestDocument.cutover.stage0_path)) -cne $canonicalStage0 -or
    ([string]$manifestDocument.cutover.stage0_sha256).ToLowerInvariant() -cne $stage0Sha -or
    $declaredLauncherSourceSha -cne $launcherSha -or
    $declaredServiceSourceSha -cne $serviceSha -or
    $declaredStage0SourceSha -cne $stage0Sha -or
    (ConvertTo-CanonicalLocalPath -ResolvedPath $expectedLauncherPath) -cne $canonicalLauncher -or
    (ConvertTo-CanonicalLocalPath -ResolvedPath $expectedServicePath) -cne $canonicalService -or
    (ConvertTo-CanonicalLocalPath -ResolvedPath $expectedStage0Path) -cne $canonicalStage0 -or
    (ConvertTo-CanonicalLocalPath -ResolvedPath $expectedHostReadyPath) -cne $canonicalManifestHostReadyReceipt -or
    (Get-FileHash -LiteralPath $launcherSource -Algorithm SHA256).Hash.ToLowerInvariant() -cne $launcherSha -or
    (Get-FileHash -LiteralPath $serviceSource -Algorithm SHA256).Hash.ToLowerInvariant() -cne $serviceSha -or
    (Get-FileHash -LiteralPath $stage0Source -Algorithm SHA256).Hash.ToLowerInvariant() -cne $stage0Sha -or
    -not (Test-CanonicalPathInsideRoot -CanonicalPath $canonicalLauncher -CanonicalRoot $canonicalActivationArtifactRoot) -or
    -not (Test-CanonicalPathInsideRoot -CanonicalPath $canonicalService -CanonicalRoot $canonicalActivationArtifactRoot) -or
    -not (Test-CanonicalPathInsideRoot -CanonicalPath $canonicalStage0 -CanonicalRoot $canonicalActivationArtifactRoot) -or
    -not @($canonicalReadRoots | Where-Object {
        Test-CanonicalPathInsideRoot -CanonicalPath $canonicalLauncherSource -CanonicalRoot $_
    }).Count -or
    -not @($canonicalReadRoots | Where-Object {
        Test-CanonicalPathInsideRoot -CanonicalPath $canonicalServiceSource -CanonicalRoot $_
    }).Count -or
    -not @($canonicalReadRoots | Where-Object {
        Test-CanonicalPathInsideRoot -CanonicalPath $canonicalStage0Source -CanonicalRoot $_
    }).Count -or
    -not @($canonicalReadRoots | Where-Object {
        Test-CanonicalPathInsideRoot -CanonicalPath $canonicalDependencyRoot -CanonicalRoot $_
    }).Count -or
    -not @($canonicalReadRoots | Where-Object {
        Test-CanonicalPathInsideRoot -CanonicalPath $canonicalManifestHostReadyReceipt -CanonicalRoot $_
    }).Count -or
    [string]$manifestDocument.cutover.singleton_policy -cne 'one_unified_candidate_host' -or
    $manifestDocument.cutover.rollback_required -ne $true
) {
    throw 'The PAPER manifest cutover binding differs from this launcher.'
}

$launcherArgumentContract = Resolve-StrictLocalPath `
    -LiteralPath ([string]$manifestDocument.cutover.launcher_arguments_path) `
    -RequireFile $true
$canonicalLauncherArgumentContract = ConvertTo-CanonicalLocalPath `
    -ResolvedPath $launcherArgumentContract
if (-not @($canonicalReadRoots | Where-Object {
    Test-CanonicalPathInsideRoot `
        -CanonicalPath $canonicalLauncherArgumentContract `
        -CanonicalRoot $_
}).Count) {
    throw 'The launcher argument contract escaped AllowedReadRoot.'
}
$declaredArgumentContractSha = (
    [string]$manifestDocument.cutover.launcher_arguments_sha256
).ToLowerInvariant()
if ($declaredArgumentContractSha -notmatch '^[0-9a-f]{64}$') {
    throw 'The launcher argument contract SHA-256 is invalid.'
}
$actualArgumentContractSha = (
    Get-FileHash -LiteralPath $launcherArgumentContract -Algorithm SHA256
).Hash.ToLowerInvariant()
if ($actualArgumentContractSha -cne $declaredArgumentContractSha) {
    throw 'The launcher argument contract content hash does not match.'
}
try {
    $argumentContract = (
        [IO.File]::ReadAllText($launcherArgumentContract) |
            ConvertFrom-Json -ErrorAction Stop
    )
}
catch {
    throw 'The launcher argument contract is not valid JSON.'
}
Assert-ExactObjectProperties `
    -Value $argumentContract `
    -Expected @('schema_version', 'invocations') `
    -Label 'launcher argument contract'
if (
    [string]$argumentContract.schema_version -cne
    'chili.captured-paper-launcher-argument-contract.v1'
) {
    throw 'The launcher argument contract schema is unsupported.'
}
Assert-ExactObjectProperties `
    -Value $argumentContract.invocations `
    -Expected @('ActivatePaper', 'NoOrderSmoke', 'ValidateOnly') `
    -Label 'launcher invocation roster'
$selectedEntry = $argumentContract.invocations.PSObject.Properties[$Mode].Value
Assert-ExactObjectProperties `
    -Value $selectedEntry `
    -Expected @('projection', 'projection_sha256') `
    -Label 'selected launcher invocation'
$expectedProjectionJson = ConvertTo-CanonicalProjectionJson `
    -Projection $selectedEntry.projection
$expectedProjectionSha = ([string]$selectedEntry.projection_sha256).ToLowerInvariant()
if (
    $expectedProjectionSha -notmatch '^[0-9a-f]{64}$' -or
    (Get-Sha256Text -Value $expectedProjectionJson) -cne $expectedProjectionSha
) {
    throw 'The selected launcher projection hash is invalid.'
}
$projectedServiceArguments = @($selectedEntry.projection.service_arguments)
$hostReadyPositions = @()
for ($index = 0; $index -lt $projectedServiceArguments.Count; $index++) {
    if ($projectedServiceArguments[$index] -ceq '--host-ready-receipt') {
        $hostReadyPositions += $index
    }
}
$hostReadyReceipt = $null
$canonicalHostReadyReceipt = $null
if ($Mode -eq 'ActivatePaper') {
    if (
        $hostReadyPositions.Count -ne 1 -or
        $hostReadyPositions[0] + 1 -ge $projectedServiceArguments.Count
    ) {
        throw 'ActivatePaper requires one sealed host-ready receipt path.'
    }
    $hostReadyReceipt = Resolve-StrictLocalOutputPath `
        -LiteralPath ([string]$projectedServiceArguments[$hostReadyPositions[0] + 1])
    if (Test-LocalPathEntryPresent -LiteralPath $hostReadyReceipt) {
        throw 'The host-ready PREPARED receipt path is append-only.'
    }
    foreach ($suffix in @(
        '.permit.json',
        '.started.json',
        '.revocation-requested.json',
        '.revoked.json',
        '.dispatch.lock'
    )) {
        if (Test-LocalPathEntryPresent -LiteralPath ($hostReadyReceipt + $suffix)) {
            throw 'A host activation handshake sibling already exists.'
        }
    }
    $canonicalHostReadyReceipt = ConvertTo-CanonicalLocalPath `
        -ResolvedPath $hostReadyReceipt
    if (-not @($canonicalReadRoots | Where-Object {
        Test-CanonicalPathInsideRoot `
            -CanonicalPath $canonicalHostReadyReceipt `
            -CanonicalRoot $_
    }).Count) {
        throw 'The host-ready receipt path escaped AllowedReadRoot.'
    }
    if ($canonicalHostReadyReceipt -cne $canonicalManifestHostReadyReceipt) {
        throw 'The projected host-ready receipt differs from the manifest cutover.'
    }
}
elseif ($hostReadyPositions.Count -ne 0) {
    throw 'Only ActivatePaper may include a host-ready receipt path.'
}
$actualProjection = New-LauncherInvocationProjection `
    -SelectedMode $Mode `
    -SelectedServiceMode $serviceMode `
    -ManifestSchemaVersion $expectedManifestSchema `
    -CanonicalCandidate $canonicalCandidate `
    -CanonicalPython $canonicalPython `
    -PythonSha256 $pythonSha `
    -CanonicalDependencyRoot $canonicalDependencyRoot `
    -DependencyRootIdentitySha256 $dependencyRootIdentitySha `
    -CanonicalReadRoots $canonicalReadRoots `
    -CanonicalLauncherSource $canonicalLauncherSource `
    -CanonicalLauncher $canonicalLauncher `
    -LauncherSha256 $launcherSha `
    -CanonicalStage0Source $canonicalStage0Source `
    -CanonicalStage0 $canonicalStage0 `
    -Stage0Sha256 $stage0Sha `
    -CanonicalServiceSource $canonicalServiceSource `
    -CanonicalService $canonicalService `
    -ServiceSha256 $serviceSha `
    -ReceiptPolicy $receiptPolicy `
    -CanonicalReceiptOutput $canonicalNoOrderReceipt `
    -CanonicalHostReadyReceipt $canonicalHostReadyReceipt
$actualProjectionJson = ConvertTo-CanonicalProjectionJson -Projection $actualProjection
$actualProjectionSha = Get-Sha256Text -Value $actualProjectionJson
if (
    $actualProjectionSha -cne $expectedProjectionSha -or
    $actualProjectionJson -cne $expectedProjectionJson
) {
    throw 'The actual launcher invocation differs from its sealed projection.'
}

# Process exclusivity must outlive an activation-manifest generation.  A mutex
# derived from the manifest hash permits an old and a freshly recertified host
# to run together, defeating the single transport/capture owner contract.  The
# service still verifies the exact manifest/launcher hashes independently; the
# cross-session mutex answers only whether *any* captured PAPER host exists.
$mutexName = 'Global\CHILI-Captured-Alpaca-PAPER-SINGLETON'
$mutex = [Threading.Mutex]::new($false, $mutexName)
$ownsMutex = $false
try {
    try {
        $ownsMutex = $mutex.WaitOne(0)
    }
    catch [Threading.AbandonedMutexException] {
        $ownsMutex = $true
    }
    if (-not $ownsMutex) {
        throw 'Another hash-bound captured Alpaca PAPER host already owns this generation.'
    }

    $finalHashes = [ordered]@{
        launcher = @($launcherPath, $launcherSha)
        launcher_source = @($launcherSource, $launcherSha)
        service = @($service, $serviceSha)
        service_source = @($serviceSource, $serviceSha)
        stage0 = @($stage0, $stage0Sha)
        stage0_source = @($stage0Source, $stage0Sha)
        python = @($python, $pythonSha)
        manifest = @($manifest, $actualManifestSha)
        launcher_arguments = @(
            $launcherArgumentContract,
            $actualArgumentContractSha
        )
    }
    foreach ($binding in $finalHashes.GetEnumerator()) {
        $finalSha = (
            Get-FileHash -LiteralPath $binding.Value[0] -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        if ($finalSha -cne $binding.Value[1]) {
            throw "A sealed launcher input drifted before foreground execution: $($binding.Key)"
        }
    }

    $arguments = @(
        '-I',
        '-S',
        '-B',
        $stage0,
        '--manifest', $manifest,
        '--manifest-sha256', $ManifestSha256.ToLowerInvariant(),
        '--candidate-root', $candidate,
        '--target-role', 'activation_service',
        '--target', $service,
        '--target-sha256', $serviceSha,
        '--',
        '--mode', $serviceMode,
        '--manifest', $manifest,
        '--manifest-sha256', $ManifestSha256.ToLowerInvariant(),
        '--candidate-root', $candidate,
        '--launcher-path', $launcherPath,
        '--launcher-sha256', $launcherSha
    )
    foreach ($root in $readRoots) {
        $arguments += @('--allow-read-root', $root)
    }
    if ($Mode -eq 'NoOrderSmoke') {
        $arguments += @('--no-order-receipt-output', $noOrderReceipt)
    }
    if ($Mode -eq 'ActivatePaper') {
        $arguments += @('--host-ready-receipt', $hostReadyReceipt)
    }

    # Foreground ownership is intentional: Task Scheduler observes the actual
    # worker lifetime and cannot report success merely because a child was
    # detached.  The absolute staged service lives outside the package root, so
    # give Python exactly one sealed import root and suppress user/PYTHONHOME
    # injection.  No inherited PYTHONPATH entry, shell interpolation, or command
    # string is accepted.
    $candidateLocationPushed = $false
    $pythonEnvironmentNames = @(
        'PYTHONPATH',
        'PYTHONHOME',
        'PYTHONSTARTUP',
        'PYTHONINSPECT',
        'PYTHONUSERBASE',
        'PYTHONNOUSERSITE'
    )
    $savedPythonEnvironment = @{}
    try {
        foreach ($name in $pythonEnvironmentNames) {
            $item = Get-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
            if ($null -ne $item) {
                $savedPythonEnvironment[$name] = [string]$item.Value
                Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction Stop
            }
        }
        $env:PYTHONPATH = $canonicalCandidate
        $env:PYTHONNOUSERSITE = '1'
        Push-Location -LiteralPath $candidate
        $candidateLocationPushed = $true
        if ($Mode -eq 'ActivatePaper') {
            # 2026-07-17: the task-owned console is invisible, which hid two
            # consecutive live PREPARED-phase stalls (only the dispatch lock
            # ever appeared).  Persist the service console next to the
            # handshake artifacts; every argument is a validated no-space
            # sealed path or base64, so array argument passing is exact.
            $serviceProcess = Start-Process -FilePath $python `
                -ArgumentList $arguments -NoNewWindow -Wait -PassThru `
                -WorkingDirectory $candidate `
                -RedirectStandardOutput ($hostReadyReceipt + '.service-stdout.log') `
                -RedirectStandardError ($hostReadyReceipt + '.service-stderr.log')
            if ($serviceProcess.ExitCode -ne 0) {
                throw "Captured Alpaca PAPER service rejected with exit code $($serviceProcess.ExitCode)"
            }
        }
        else {
            & $python @arguments
            if ($LASTEXITCODE -ne 0) {
                throw "Captured Alpaca PAPER service rejected with exit code $LASTEXITCODE"
            }
        }
    }
    finally {
        if ($candidateLocationPushed) {
            Pop-Location
        }
        foreach ($name in $pythonEnvironmentNames) {
            Remove-Item -LiteralPath ("Env:" + $name) -ErrorAction SilentlyContinue
            if ($savedPythonEnvironment.ContainsKey($name)) {
                Set-Item -LiteralPath ("Env:" + $name) `
                    -Value $savedPythonEnvironment[$name] -ErrorAction Stop
            }
        }
    }
}
finally {
    if ($ownsMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
