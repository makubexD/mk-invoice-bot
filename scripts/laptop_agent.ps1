<#
.SYNOPSIS
    The laptop agent for `laptop_outlook` jobs (R-7.14, S11.14).

.DESCRIPTION
    Reads the job files the bot writes to the personal OneDrive `_outbox/` and,
    in later steps, sends each one through Outlook classic and writes a receipt.
    This step only checks a job: its size, its HMAC, its recipients and its
    attachments.

    The functions are dot-sourceable so the Pester tests can call them without
    starting Outlook. The main block runs only when the script is invoked.

.PARAMETER OneDriveRoot
    The personal OneDrive root; job files live in its `_outbox/` folder and an
    invoice folder is relative to it (D111).

.PARAMETER WorkRoot
    The work OneDrive root the agent mirrors folders into (step 36).

.PARAMETER KeyFile
    A file holding the shared `laptop_agent_key`; the HMAC is computed with it.

.PARAMETER AllowlistFile
    A file of allowed recipients, one address per line; blank lines and lines
    starting with `#` are ignored.

.PARAMETER DryRun
    Log what would be sent and change nothing (step 35).
#>
[CmdletBinding()]
param(
    [string] $OneDriveRoot = '',
    [string] $WorkRoot = '',
    [string] $KeyFile = '',
    [string] $AllowlistFile = '',
    [switch] $DryRun
)

#: The largest job file the agent reads (64 KiB, R-7.14).
$script:MaxJobBytes = 65536

#: A job id is 32 lowercase hex characters, the form of `uuid4().hex`.
$script:JobIdPattern = '^[0-9a-f]{32}$'

function ConvertTo-CanonicalString {
    <#
    .SYNOPSIS
        Return one JSON string literal exactly as Python's `json.dumps(..., ensure_ascii=True)`.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string] $Text
    )

    $builder = [System.Text.StringBuilder]::new()
    [void] $builder.Append('"')
    foreach ($character in $Text.ToCharArray()) {
        $code = [int] $character
        if ($code -eq 0x22) {
            [void] $builder.Append('\"')
        }
        elseif ($code -eq 0x5C) {
            [void] $builder.Append('\\')
        }
        elseif ($code -eq 0x08) {
            [void] $builder.Append('\b')
        }
        elseif ($code -eq 0x0C) {
            [void] $builder.Append('\f')
        }
        elseif ($code -eq 0x0A) {
            [void] $builder.Append('\n')
        }
        elseif ($code -eq 0x0D) {
            [void] $builder.Append('\r')
        }
        elseif ($code -eq 0x09) {
            [void] $builder.Append('\t')
        }
        elseif ($code -lt 0x20 -or $code -ge 0x7F) {
            # Lowercase hex, and UTF-16 code units, so a surrogate pair matches Python too.
            [void] $builder.Append('\u').Append($code.ToString('x4'))
        }
        else {
            [void] $builder.Append($character)
        }
    }
    [void] $builder.Append('"')
    return $builder.ToString()
}

function ConvertTo-CanonicalJson {
    <#
    .SYNOPSIS
        Return canonical JSON: sorted keys, no spaces, ASCII escapes.
    .DESCRIPTION
        The same bytes as Python's `json.dumps(value, sort_keys=True,
        separators=(",", ":"), ensure_ascii=True)`, which signs the job.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowNull()]
        $Value
    )

    if ($null -eq $Value) {
        return 'null'
    }
    if ($Value -is [bool]) {
        if ($Value) { return 'true' }
        return 'false'
    }
    if ($Value -is [string]) {
        return ConvertTo-CanonicalString -Text $Value
    }
    if ($Value -is [int] -or $Value -is [long] -or $Value -is [decimal] -or
        $Value -is [double] -or $Value -is [single]) {
        return $Value.ToString([System.Globalization.CultureInfo]::InvariantCulture)
    }

    $isDictionary = $Value -is [System.Collections.IDictionary]
    $isObject = $Value -is [System.Management.Automation.PSCustomObject]
    if ($isDictionary -or $isObject) {
        if ($isDictionary) {
            $names = [string[]] @($Value.Keys)
        }
        else {
            $names = [string[]] @($Value.PSObject.Properties.Name)
        }
        [Array]::Sort($names, [System.StringComparer]::Ordinal)
        $members = foreach ($name in $names) {
            if ($isDictionary) {
                $memberValue = $Value[$name]
            }
            else {
                $memberValue = $Value.PSObject.Properties[$name].Value
            }
            $member = ConvertTo-CanonicalJson -Value $memberValue
            '{0}:{1}' -f (ConvertTo-CanonicalString -Text $name), $member
        }
        return '{' + ($members -join ',') + '}'
    }

    if ($Value -is [System.Collections.IEnumerable]) {
        $items = foreach ($item in $Value) {
            ConvertTo-CanonicalJson -Value $item
        }
        return '[' + (@($items) -join ',') + ']'
    }

    throw "cannot canonicalize a value of type $($Value.GetType().FullName)"
}

function ConvertFrom-JobJson {
    <#
    .SYNOPSIS
        Parse JSON text without turning date-looking strings into dates.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string] $Text
    )

    # PowerShell 7.5+ converts ISO 8601 strings to DateTime by default, which
    # would change the signed bytes. Older versions already keep strings.
    $parameters = @{ InputObject = $Text }
    if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) {
        $parameters['DateKind'] = 'String'
    }
    return ConvertFrom-Json @parameters
}

function Get-JobSignature {
    <#
    .SYNOPSIS
        Return the lowercase hex HMAC-SHA256 of a job's fields under `-Key`.
    .DESCRIPTION
        Signs every field except `hmac`, over the canonical JSON of the rest.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        $Job,
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string] $Key
    )

    $fields = [ordered] @{}
    foreach ($property in $Job.PSObject.Properties) {
        if ($property.Name -ne 'hmac') {
            $fields[$property.Name] = $property.Value
        }
    }
    $canonical = ConvertTo-CanonicalJson -Value $fields
    $algorithm = [System.Security.Cryptography.HMACSHA256]::new(
        [System.Text.Encoding]::UTF8.GetBytes($Key)
    )
    try {
        $digest = $algorithm.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($canonical))
    }
    finally {
        $algorithm.Dispose()
    }
    return [System.Convert]::ToHexString($digest).ToLowerInvariant()
}

function Test-SignatureMatch {
    <#
    .SYNOPSIS
        Compare two hex digests in constant time.
    #>
    [CmdletBinding()]
    param(
        [AllowNull()]
        [AllowEmptyString()]
        [string] $Expected,
        [AllowNull()]
        [AllowEmptyString()]
        [string] $Actual
    )

    if ([string]::IsNullOrEmpty($Expected) -or [string]::IsNullOrEmpty($Actual)) {
        return $false
    }
    try {
        $expectedBytes = [System.Convert]::FromHexString($Expected)
        $actualBytes = [System.Convert]::FromHexString($Actual)
    }
    catch {
        return $false
    }
    return [System.Security.Cryptography.CryptographicOperations]::FixedTimeEquals(
        $expectedBytes,
        $actualBytes
    )
}

function Test-PlainFileName {
    <#
    .SYNOPSIS
        Return whether a name is a file name inside a folder, not a path.
    #>
    [CmdletBinding()]
    param(
        [AllowNull()]
        [AllowEmptyString()]
        [string] $Name
    )

    if ([string]::IsNullOrEmpty($Name)) {
        return $false
    }
    if ($Name -eq '.' -or $Name -eq '..') {
        return $false
    }
    foreach ($forbidden in @('/', '\', ':', [char] 0)) {
        if ($Name.Contains($forbidden)) {
            return $false
        }
    }
    return $true
}

function Write-JobRefusal {
    <#
    .SYNOPSIS
        Log why a job was refused: its id and the reason, never its content.
    #>
    [CmdletBinding()]
    param(
        [AllowNull()]
        [AllowEmptyString()]
        [string] $JobId,
        [Parameter(Mandatory)]
        [string] $Reason
    )

    $label = if ([string]::IsNullOrEmpty($JobId)) { 'unknown' } else { $JobId }
    Write-Warning ("job {0}: {1}" -f $label, $Reason)
}

function Test-JobFile {
    <#
    .SYNOPSIS
        Return $true only for a job file the agent may act on (R-7.14).
    .DESCRIPTION
        The file must be under 64 KiB and valid JSON, its HMAC must match, every
        recipient must be on the allowlist, and every attachment must be a plain
        file name inside the invoice folder. A refusal logs the job id and the
        reason only.
    .PARAMETER Path
        The job file in `_outbox/`.
    .PARAMETER Key
        The shared key text; its UTF-8 bytes sign the job.
    .PARAMETER Allowlist
        The allowed recipient addresses.
    .PARAMETER Folder
        The invoice folder, already joined to the OneDrive root (D111).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $Path,
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string] $Key,
        [AllowNull()]
        [string[]] $Allowlist,
        [AllowNull()]
        [AllowEmptyString()]
        [string] $Folder
    )

    $file = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $file -or $file.PSIsContainer) {
        Write-JobRefusal -JobId '' -Reason 'job file is missing'
        return $false
    }
    if ($file.Length -ge $script:MaxJobBytes) {
        Write-JobRefusal -JobId '' -Reason 'job file is too large'
        return $false
    }

    try {
        $text = [System.IO.File]::ReadAllText($file.FullName, [System.Text.Encoding]::UTF8)
        $job = ConvertFrom-JobJson -Text $text
    }
    catch {
        Write-JobRefusal -JobId '' -Reason 'job file is not valid JSON'
        return $false
    }
    if ($null -eq $job -or $job -isnot [System.Management.Automation.PSCustomObject]) {
        Write-JobRefusal -JobId '' -Reason 'job file is not a JSON object'
        return $false
    }

    $jobId = $null
    $idProperty = $job.PSObject.Properties['job_id']
    if ($null -ne $idProperty) {
        $jobId = [string] $idProperty.Value
    }
    if ($jobId -notmatch $script:JobIdPattern) {
        Write-JobRefusal -JobId $jobId -Reason 'job id is invalid'
        return $false
    }

    $hmacProperty = $job.PSObject.Properties['hmac']
    if ($null -eq $hmacProperty -or $hmacProperty.Value -isnot [string]) {
        Write-JobRefusal -JobId $jobId -Reason 'hmac is missing'
        return $false
    }
    try {
        $actual = Get-JobSignature -Job $job -Key $Key
    }
    catch {
        Write-JobRefusal -JobId $jobId -Reason 'job fields cannot be signed'
        return $false
    }
    if (-not (Test-SignatureMatch -Expected $hmacProperty.Value -Actual $actual)) {
        Write-JobRefusal -JobId $jobId -Reason 'hmac does not verify'
        return $false
    }

    $recipients = @($job.to)
    if ($recipients.Count -eq 0) {
        Write-JobRefusal -JobId $jobId -Reason 'job has no recipient'
        return $false
    }
    foreach ($recipient in $recipients) {
        if ($recipient -isnot [string] -or -not (@($Allowlist) -contains $recipient)) {
            Write-JobRefusal -JobId $jobId -Reason 'recipient is not allowlisted'
            return $false
        }
    }

    $folderItem = $null
    if (-not [string]::IsNullOrEmpty($Folder)) {
        $folderItem = Get-Item -LiteralPath $Folder -ErrorAction SilentlyContinue
    }
    if ($null -eq $folderItem -or -not $folderItem.PSIsContainer) {
        Write-JobRefusal -JobId $jobId -Reason 'invoice folder is missing'
        return $false
    }
    $folderPath = [System.IO.Path]::GetFullPath($folderItem.FullName)

    foreach ($attachment in @($job.attachments)) {
        if ($attachment -isnot [string] -or -not (Test-PlainFileName -Name $attachment)) {
            Write-JobRefusal -JobId $jobId -Reason 'attachment is not a plain file name'
            return $false
        }
        $candidate = Join-Path -Path $folderPath -ChildPath $attachment
        $resolved = Get-Item -LiteralPath $candidate -ErrorAction SilentlyContinue
        if ($null -eq $resolved -or $resolved.PSIsContainer) {
            Write-JobRefusal -JobId $jobId -Reason 'attachment is not in the invoice folder'
            return $false
        }
        $parent = [System.IO.Path]::GetFullPath([System.IO.Path]::GetDirectoryName($resolved.FullName))
        if (-not [string]::Equals($parent, $folderPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            Write-JobRefusal -JobId $jobId -Reason 'attachment is not in the invoice folder'
            return $false
        }
    }

    return $true
}

if ($MyInvocation.InvocationName -ne '.') {
    foreach ($parameterName in @('OneDriveRoot', 'WorkRoot', 'KeyFile', 'AllowlistFile')) {
        if ([string]::IsNullOrWhiteSpace((Get-Variable -Name $parameterName -ValueOnly))) {
            throw "missing required parameter -$parameterName"
        }
    }
    if (-not (Test-Path -LiteralPath $OneDriveRoot -PathType Container)) {
        throw "OneDrive root not found: $OneDriveRoot"
    }
    if (-not (Test-Path -LiteralPath $KeyFile -PathType Leaf)) {
        throw "key file not found: $KeyFile"
    }
    if (-not (Test-Path -LiteralPath $AllowlistFile -PathType Leaf)) {
        throw "allowlist file not found: $AllowlistFile"
    }

    $agentKey = (Get-Content -LiteralPath $KeyFile -Raw).TrimEnd("`r", "`n")
    $agentAllowlist = @(
        Get-Content -LiteralPath $AllowlistFile |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -and -not $_.StartsWith('#') }
    )
    $outbox = Join-Path -Path $OneDriveRoot -ChildPath '_outbox'
    foreach ($jobFile in @(Get-ChildItem -LiteralPath $outbox -Filter '*.json' -File -ErrorAction SilentlyContinue)) {
        $folder = ''
        try {
            # Read the folder only from a file under the cap; Test-JobFile refuses the rest.
            if ($jobFile.Length -ge $script:MaxJobBytes) {
                throw 'job file is too large'
            }
            $job = ConvertFrom-JobJson -Text ([System.IO.File]::ReadAllText($jobFile.FullName, [System.Text.Encoding]::UTF8))
            $folderProperty = $job.PSObject.Properties['folder']
            if ($null -ne $folderProperty -and $folderProperty.Value -is [string]) {
                $folder = Join-Path -Path $OneDriveRoot -ChildPath $folderProperty.Value
            }
        }
        catch {
            $folder = ''
        }
        $isValid = Test-JobFile -Path $jobFile.FullName -Key $agentKey -Allowlist $agentAllowlist -Folder $folder
        if ($isValid) {
            Write-Verbose ("job {0} is valid" -f $jobFile.BaseName)
        }
    }
}
