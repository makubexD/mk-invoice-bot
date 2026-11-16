<#
.SYNOPSIS
    The laptop agent for `laptop_outlook` jobs (R-7.14, S11.14).

.DESCRIPTION
    Reads the job files the bot writes to the personal OneDrive `_outbox/`,
    refuses any that fail the checks (size, HMAC, recipients, attachments),
    sends the rest through Outlook classic and writes a receipt for each.

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

#: The longest error text a receipt carries (matches `MAX_ERROR_LENGTH`).
$script:MaxErrorLength = 500

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

function Write-AgentLog {
    <#
    .SYNOPSIS
        Append one UTC-timestamped line to the agent's local log.
    .DESCRIPTION
        Never pass a recipient, subject or body: the log only holds job ids,
        outcomes and counts (R-7.14).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string] $Message,
        [string] $LogFile = ''
    )

    if ([string]::IsNullOrEmpty($LogFile)) {
        $LogFile = Join-Path -Path $WorkRoot -ChildPath 'agent.log'
    }
    $line = '{0} {1}{2}' -f [DateTime]::UtcNow.ToString('o'), $Message, [Environment]::NewLine
    [System.IO.File]::AppendAllText($LogFile, $line, [System.Text.UTF8Encoding]::new($false))
}

function Test-JobIdInState {
    <#
    .SYNOPSIS
        Return whether a job id was already sent, according to the local state file.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $StateFile,
        [Parameter(Mandatory)]
        [string] $JobId
    )

    if (-not (Test-Path -LiteralPath $StateFile -PathType Leaf)) {
        return $false
    }
    foreach ($line in @(Get-Content -LiteralPath $StateFile)) {
        if ($line.Trim() -eq $JobId) {
            return $true
        }
    }
    return $false
}

function Remove-JobIdFromState {
    <#
    .SYNOPSIS
        Drop a job id from the state file after a send that failed.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $StateFile,
        [Parameter(Mandatory)]
        [string] $JobId
    )

    if (-not (Test-Path -LiteralPath $StateFile -PathType Leaf)) {
        return
    }
    $remaining = @(Get-Content -LiteralPath $StateFile | Where-Object { $_.Trim() -ne $JobId })
    [System.IO.File]::WriteAllLines($StateFile, [string[]] $remaining, [System.Text.UTF8Encoding]::new($false))
}

function Write-JobReceipt {
    <#
    .SYNOPSIS
        Write `_outbox/receipts/<job id>.json` atomically (R-7.14).
    .DESCRIPTION
        The receipt has exactly `job_id` and `sent_at`, or `job_id` and
        `error`, so it parses with the bot's `parse_receipt`.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $ReceiptsFolder,
        [Parameter(Mandatory)]
        [string] $JobId,
        [AllowNull()]
        [string] $SentAt,
        [AllowNull()]
        [string] $ErrorText
    )

    if (-not (Test-Path -LiteralPath $ReceiptsFolder -PathType Container)) {
        New-Item -ItemType Directory -Path $ReceiptsFolder -Force | Out-Null
    }
    if ($PSBoundParameters.ContainsKey('SentAt')) {
        $payload = [ordered] @{ job_id = $JobId; sent_at = $SentAt }
    }
    else {
        $payload = [ordered] @{ job_id = $JobId; error = [string] $ErrorText }
    }
    $json = ConvertTo-Json -InputObject $payload -Compress
    $tempName = '{0}.{1}.tmp' -f $JobId, [guid]::NewGuid().ToString('N')
    $tempPath = Join-Path -Path $ReceiptsFolder -ChildPath $tempName
    [System.IO.File]::WriteAllText($tempPath, $json, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tempPath -Destination (Join-Path -Path $ReceiptsFolder -ChildPath ($JobId + '.json')) -Force
}

function Send-Job {
    <#
    .SYNOPSIS
        Send one validated job through Outlook and write its receipt (R-7.14).
    .DESCRIPTION
        A job id already in the state file is never sent again: the receipt is
        rewritten. Otherwise the id is appended to the state file before
        `Send()`, so a crash cannot send twice; if the send throws, the id is
        removed again and an error receipt is written. The job file is deleted
        after its receipt, whatever the outcome: an error is final and the bot
        asks the sender what to do (BR-D11).
    .PARAMETER Job
        The parsed job object from `_outbox/<job id>.json`.
    .PARAMETER Folder
        The invoice folder the attachments are read from, already joined to
        the OneDrive root (D111).
    .PARAMETER Outlook
        The Outlook COM application, or `$null` with `-DryRun`.
    .PARAMETER StateFile
        The local `sent-jobs.txt`, one sent job id per line.
    .PARAMETER Outbox
        The `_outbox/` folder: the job file and `receipts/` live there.
    .PARAMETER LogFile
        The log to append to; defaults to `$WorkRoot/agent.log`.
    .PARAMETER DryRun
        Log what would be sent and touch no file except the log.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        $Job,
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string] $Folder,
        [AllowNull()]
        $Outlook,
        [Parameter(Mandatory)]
        [string] $StateFile,
        [Parameter(Mandatory)]
        [string] $Outbox,
        [string] $LogFile = '',
        [switch] $DryRun
    )

    $jobId = [string] $Job.job_id
    $attachments = @($Job.attachments)
    if ([string]::IsNullOrEmpty($LogFile)) {
        $LogFile = Join-Path -Path $WorkRoot -ChildPath 'agent.log'
    }
    $jobFile = Join-Path -Path $Outbox -ChildPath ($jobId + '.json')
    $receiptsFolder = Join-Path -Path $Outbox -ChildPath 'receipts'

    if ($DryRun) {
        Write-AgentLog -LogFile $LogFile -Message (
            'would send job {0} with {1} attachments' -f $jobId, $attachments.Count
        )
        return
    }

    if (Test-JobIdInState -StateFile $StateFile -JobId $jobId) {
        Write-JobReceipt -ReceiptsFolder $receiptsFolder -JobId $jobId `
            -SentAt ([DateTime]::UtcNow.ToString('o'))
        Write-AgentLog -LogFile $LogFile -Message (
            'job {0}: already sent, receipt rewritten ({1} attachments)' -f $jobId, $attachments.Count
        )
        Remove-Item -LiteralPath $jobFile -Force -ErrorAction SilentlyContinue
        return
    }

    $stateAppended = $false
    $sendError = $null
    try {
        [System.IO.File]::AppendAllText(
            $StateFile,
            $jobId + [Environment]::NewLine,
            [System.Text.UTF8Encoding]::new($false)
        )
        $stateAppended = $true
        $mail = $Outlook.CreateItem(0)
        $mail.To = @($Job.to) -join '; '
        $mail.Subject = [string] $Job.subject
        $mail.Body = [string] $Job.body
        foreach ($name in $attachments) {
            [void] $mail.Attachments.Add((Join-Path -Path $Folder -ChildPath $name))
        }
        $mail.Send()
    }
    catch {
        $sendError = $_
    }

    if ($null -ne $sendError) {
        if ($stateAppended) {
            Remove-JobIdFromState -StateFile $StateFile -JobId $jobId
        }
        $errorText = '{0}: {1}' -f $sendError.Exception.GetType().Name, $sendError.Exception.Message
        if ($errorText.Length -gt $script:MaxErrorLength) {
            $errorText = $errorText.Substring(0, $script:MaxErrorLength)
        }
        Write-JobReceipt -ReceiptsFolder $receiptsFolder -JobId $jobId -ErrorText $errorText
        Write-AgentLog -LogFile $LogFile -Message (
            'job {0}: error ({1}), {2} attachments' -f
                $jobId, $sendError.Exception.GetType().Name, $attachments.Count
        )
    }
    else {
        Write-JobReceipt -ReceiptsFolder $receiptsFolder -JobId $jobId `
            -SentAt ([DateTime]::UtcNow.ToString('o'))
        Write-AgentLog -LogFile $LogFile -Message (
            'job {0}: sent ({1} attachments)' -f $jobId, $attachments.Count
        )
    }

    Remove-Item -LiteralPath $jobFile -Force -ErrorAction SilentlyContinue
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
    $validJobs = [System.Collections.Generic.List[object]]::new()
    foreach ($jobFile in @(Get-ChildItem -LiteralPath $outbox -Filter '*.json' -File -ErrorAction SilentlyContinue)) {
        $folder = ''
        $job = $null
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
            $job = $null
        }
        $isValid = Test-JobFile -Path $jobFile.FullName -Key $agentKey -Allowlist $agentAllowlist -Folder $folder
        if ($isValid) {
            Write-Verbose ("job {0} is valid" -f $jobFile.BaseName)
            $validJobs.Add([pscustomobject] @{ Job = $job; Folder = $folder })
        }
    }

    if ($validJobs.Count -gt 0) {
        $stateFile = Join-Path -Path $PSScriptRoot -ChildPath 'sent-jobs.txt'
        $agentLog = Join-Path -Path $WorkRoot -ChildPath 'agent.log'
        $outlook = $null
        if (-not $DryRun) {
            $outlook = New-Object -ComObject Outlook.Application
        }
        foreach ($validJob in $validJobs) {
            Send-Job -Job $validJob.Job -Folder $validJob.Folder -Outlook $outlook `
                -StateFile $stateFile -Outbox $outbox -LogFile $agentLog -DryRun:$DryRun
        }
    }
}
