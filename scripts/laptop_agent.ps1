<#
.SYNOPSIS
    The laptop agent for `laptop_outlook` jobs (R-7.14, S11.14).

.DESCRIPTION
    Reads the job files the bot writes to the personal OneDrive `_outbox/`,
    refuses any that fail the checks (size, HMAC, recipients, attachments),
    sends the rest through Outlook classic and writes a receipt for each. It
    also mirrors the personal invoice folders into the work OneDrive, matching
    them by the manifest's invoice id (R-7.3, R-7.14).

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
    Log what would be sent or copied and change nothing (steps 35, 36).
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
$script:JobIdPattern = '^[0-9a-f]{32}\z'

#: The longest error text a receipt carries (matches `MAX_ERROR_LENGTH`).
$script:MaxErrorLength = 500

#: The manifest the bot writes into every invoice folder (R-7.2).
$script:ManifestName = '.invoicebot.json'

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

function Get-FieldsSignature {
    <#
    .SYNOPSIS
        Return the lowercase hex HMAC-SHA256 of a payload's fields under `-Key`.
    .DESCRIPTION
        Signs every field except `hmac`, over the canonical JSON of the rest.
        Jobs and receipts are signed the same way (R-7.14).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        $Fields,
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string] $Key
    )

    $signed = [ordered] @{}
    foreach ($property in $Fields.PSObject.Properties) {
        if ($property.Name -ne 'hmac') {
            $signed[$property.Name] = $property.Value
        }
    }
    $canonical = ConvertTo-CanonicalJson -Value $signed
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

    return Get-FieldsSignature -Fields $Job -Key $Key
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
        Return the verified job object for a job file the agent may act on, or
        `$null` (R-7.14).
    .DESCRIPTION
        The file's bytes are read once and the parsed, checked object is
        returned, so the agent sends exactly what it verified. The file must be
        under 64 KiB and valid JSON, its name must be `<job_id>.json`, its
        HMAC must match, every recipient must be on the allowlist, `folder`
        must be a plain relative path, and every attachment must be a plain
        file name inside that folder. A refusal logs the job id and the reason
        only.
    .PARAMETER Path
        The job file in `_outbox/`.
    .PARAMETER Key
        The shared key text; its UTF-8 bytes sign the job.
    .PARAMETER Allowlist
        The allowed recipient addresses.
    .PARAMETER Root
        The personal OneDrive root the job's `folder` is relative to (D111).
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
        [Parameter(Mandatory)]
        [string] $Root
    )

    $file = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $file -or $file.PSIsContainer) {
        Write-JobRefusal -JobId '' -Reason 'job file is missing'
        return $null
    }
    # Refuse an oversized file before reading it; the byte count is checked again below.
    if ($file.Length -ge $script:MaxJobBytes) {
        Write-JobRefusal -JobId '' -Reason 'job file is too large'
        return $null
    }
    try {
        $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    }
    catch {
        Write-JobRefusal -JobId '' -Reason 'job file is missing'
        return $null
    }
    if ($bytes.Length -ge $script:MaxJobBytes) {
        Write-JobRefusal -JobId '' -Reason 'job file is too large'
        return $null
    }

    try {
        $job = ConvertFrom-JobJson -Text ([System.Text.Encoding]::UTF8.GetString($bytes))
    }
    catch {
        Write-JobRefusal -JobId '' -Reason 'job file is not valid JSON'
        return $null
    }
    if ($null -eq $job -or $job -isnot [System.Management.Automation.PSCustomObject]) {
        Write-JobRefusal -JobId '' -Reason 'job file is not a JSON object'
        return $null
    }

    $jobId = $null
    $idProperty = $job.PSObject.Properties['job_id']
    if ($null -ne $idProperty) {
        $jobId = [string] $idProperty.Value
    }
    if ($jobId -cnotmatch $script:JobIdPattern) {
        Write-JobRefusal -JobId $jobId -Reason 'job id is invalid'
        return $null
    }
    if (-not [string]::Equals($file.Name, $jobId + '.json', [System.StringComparison]::Ordinal)) {
        Write-JobRefusal -JobId $jobId -Reason 'job file name does not match job id'
        return $null
    }

    $hmacProperty = $job.PSObject.Properties['hmac']
    if ($null -eq $hmacProperty -or $hmacProperty.Value -isnot [string]) {
        Write-JobRefusal -JobId $jobId -Reason 'hmac is missing'
        return $null
    }
    try {
        $actual = Get-JobSignature -Job $job -Key $Key
    }
    catch {
        Write-JobRefusal -JobId $jobId -Reason 'job fields cannot be signed'
        return $null
    }
    if (-not (Test-SignatureMatch -Expected $hmacProperty.Value -Actual $actual)) {
        Write-JobRefusal -JobId $jobId -Reason 'hmac does not verify'
        return $null
    }

    $recipients = @($job.to)
    if ($recipients.Count -eq 0) {
        Write-JobRefusal -JobId $jobId -Reason 'job has no recipient'
        return $null
    }
    foreach ($recipient in $recipients) {
        if ($recipient -isnot [string] -or -not (@($Allowlist) -contains $recipient)) {
            Write-JobRefusal -JobId $jobId -Reason 'recipient is not allowlisted'
            return $null
        }
    }

    $folderProperty = $job.PSObject.Properties['folder']
    $relativeFolder = $null
    if ($null -ne $folderProperty -and $folderProperty.Value -is [string]) {
        $relativeFolder = $folderProperty.Value
    }
    if ([string]::IsNullOrEmpty($relativeFolder)) {
        Write-JobRefusal -JobId $jobId -Reason 'folder is not a plain relative path'
        return $null
    }
    foreach ($segment in $relativeFolder.Split('/')) {
        if (-not (Test-PlainFileName -Name $segment)) {
            Write-JobRefusal -JobId $jobId -Reason 'folder is not a plain relative path'
            return $null
        }
    }

    $folderItem = Get-Item -LiteralPath (Join-RootPath -Root $Root -Relative $relativeFolder) `
        -ErrorAction SilentlyContinue
    if ($null -eq $folderItem -or -not $folderItem.PSIsContainer) {
        Write-JobRefusal -JobId $jobId -Reason 'invoice folder is missing'
        return $null
    }
    $folderPath = [System.IO.Path]::GetFullPath($folderItem.FullName)

    foreach ($attachment in @($job.attachments)) {
        if ($attachment -isnot [string] -or -not (Test-PlainFileName -Name $attachment)) {
            Write-JobRefusal -JobId $jobId -Reason 'attachment is not a plain file name'
            return $null
        }
        $candidate = Join-Path -Path $folderPath -ChildPath $attachment
        $resolved = Get-Item -LiteralPath $candidate -ErrorAction SilentlyContinue
        if ($null -eq $resolved -or $resolved.PSIsContainer) {
            Write-JobRefusal -JobId $jobId -Reason 'attachment is not in the invoice folder'
            return $null
        }
        $parent = [System.IO.Path]::GetFullPath([System.IO.Path]::GetDirectoryName($resolved.FullName))
        if (-not [string]::Equals($parent, $folderPath, [System.StringComparison]::OrdinalIgnoreCase)) {
            Write-JobRefusal -JobId $jobId -Reason 'attachment is not in the invoice folder'
            return $null
        }
    }

    return $job
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
        $LogFile = Join-Path -Path $PSScriptRoot -ChildPath 'agent.log'
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
        The receipt has exactly `job_id`, `sent_at` and `hmac`, or `job_id`,
        `error` and `hmac`, so it parses with the bot's `parse_receipt`. The
        HMAC signs the other fields the way `Get-FieldsSignature` signs a job.
    .PARAMETER Key
        The shared secret; only the bot and the laptop agent hold it.
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
        [string] $ErrorText,
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string] $Key
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
    $payload['hmac'] = Get-FieldsSignature -Fields ([pscustomobject] $payload) -Key $Key
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
        removed again and an error receipt is written. The job file that
        `Test-JobFile` read is deleted after its receipt, whatever the outcome:
        an error is final and the bot asks the sender what to do (BR-D11).
    .PARAMETER Job
        The parsed, verified job object `Test-JobFile` returned.
    .PARAMETER Path
        The job file that object was read from; it is deleted after the
        receipt.
    .PARAMETER Folder
        The invoice folder the attachments are read from, already joined to
        the OneDrive root (D111).
    .PARAMETER Outlook
        The Outlook COM application, or `$null` with `-DryRun`.
    .PARAMETER StateFile
        The local `sent-jobs.txt`, one sent job id per line.
    .PARAMETER Outbox
        The `_outbox/` folder: `receipts/` live there.
    .PARAMETER Key
        The shared secret the receipt is signed with (R-7.14).
    .PARAMETER LogFile
        The log to append to; defaults to `<script folder>/agent.log`.
    .PARAMETER DryRun
        Log what would be sent and touch no file except the log.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        $Job,
        [Parameter(Mandatory)]
        [string] $Path,
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string] $Folder,
        [AllowNull()]
        $Outlook,
        [Parameter(Mandatory)]
        [string] $StateFile,
        [Parameter(Mandatory)]
        [string] $Outbox,
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string] $Key,
        [string] $LogFile = '',
        [switch] $DryRun
    )

    $jobId = [string] $Job.job_id
    $attachments = @($Job.attachments)
    if ([string]::IsNullOrEmpty($LogFile)) {
        $LogFile = Join-Path -Path $PSScriptRoot -ChildPath 'agent.log'
    }
    $receiptsFolder = Join-Path -Path $Outbox -ChildPath 'receipts'

    if ($DryRun) {
        Write-AgentLog -LogFile $LogFile -Message (
            'would send job {0} with {1} attachments' -f $jobId, $attachments.Count
        )
        return
    }

    if (Test-JobIdInState -StateFile $StateFile -JobId $jobId) {
        Write-JobReceipt -ReceiptsFolder $receiptsFolder -JobId $jobId `
            -SentAt ([DateTime]::UtcNow.ToString('o')) -Key $Key
        Write-AgentLog -LogFile $LogFile -Message (
            'job {0}: already sent, receipt rewritten ({1} attachments)' -f $jobId, $attachments.Count
        )
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
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
        Write-JobReceipt -ReceiptsFolder $receiptsFolder -JobId $jobId -ErrorText $errorText -Key $Key
        Write-AgentLog -LogFile $LogFile -Message (
            'job {0}: error ({1}), {2} attachments' -f
                $jobId, $sendError.Exception.GetType().Name, $attachments.Count
        )
    }
    else {
        Write-JobReceipt -ReceiptsFolder $receiptsFolder -JobId $jobId `
            -SentAt ([DateTime]::UtcNow.ToString('o')) -Key $Key
        Write-AgentLog -LogFile $LogFile -Message (
            'job {0}: sent ({1} attachments)' -f $jobId, $attachments.Count
        )
    }

    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
}

function Join-RootPath {
    <#
    .SYNOPSIS
        Join a root and a relative folder path that uses `/` separators.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $Root,
        [Parameter(Mandatory)]
        [string] $Relative
    )

    $path = $Root
    foreach ($segment in $Relative.Split('/')) {
        $path = Join-Path -Path $path -ChildPath $segment
    }
    return $path
}

function Get-RelativeFolderPath {
    <#
    .SYNOPSIS
        Return a path relative to the root, with `/` separators.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $Root,
        [Parameter(Mandatory)]
        [string] $Path
    )

    return [System.IO.Path]::GetRelativePath($Root, $Path).Replace('\', '/')
}

function Test-SameRelativePath {
    <#
    .SYNOPSIS
        Compare two relative folder paths the way Windows compares paths.
    #>
    [CmdletBinding()]
    param(
        [AllowNull()]
        [string] $Left,
        [AllowNull()]
        [string] $Right
    )

    return [string]::Equals($Left, $Right, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-IntegerId {
    <#
    .SYNOPSIS
        Return whether a parsed JSON value is an integer, as the manifest requires.
    #>
    [CmdletBinding()]
    param(
        [AllowNull()]
        $Value
    )

    if ($null -eq $Value -or $Value -is [bool]) {
        return $false
    }
    return $Value -is [int] -or $Value -is [long] -or $Value -is [int16] -or $Value -is [byte]
}

function Get-InvoiceFolderCandidates {
    <#
    .SYNOPSIS
        Return the folders that may be invoice folders: the root's children and
        the children of its year folders (a name of four digits), no deeper.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $Root
    )

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        return
    }
    foreach ($child in @(Get-ChildItem -LiteralPath $Root -Directory -Force -ErrorAction SilentlyContinue)) {
        # `_outbox/` and OneDrive's own `_` folders are never invoice folders.
        if ($child.Name.StartsWith('_')) {
            continue
        }
        if ($child.Name -match '^\d{4}$') {
            foreach ($grandchild in @(Get-ChildItem -LiteralPath $child.FullName -Directory -Force -ErrorAction SilentlyContinue)) {
                $grandchild
            }
        }
        else {
            $child
        }
    }
}

function Read-InvoiceManifest {
    <#
    .SYNOPSIS
        Return the invoice id in a folder's `.invoicebot.json`, or `$null`.
    .DESCRIPTION
        A folder without a manifest is left alone silently. A manifest that is
        too large, unreadable or without an integer `invoice_id` is logged and
        skipped. The 64 KiB cap is the one job files use.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $Folder,
        [Parameter(Mandatory)]
        [string] $Relative,
        [string] $LogFile = ''
    )

    $manifestPath = Join-Path -Path $Folder -ChildPath $script:ManifestName
    # `-Force` so a dotfile is found on hosts that map it to a hidden item.
    $manifestFile = Get-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
    if ($null -eq $manifestFile -or $manifestFile.PSIsContainer) {
        return
    }
    if ($manifestFile.Length -ge $script:MaxJobBytes) {
        Write-AgentLog -LogFile $LogFile -Message (
            'folder {0}: manifest is too large, skipped' -f $Relative
        )
        return
    }
    try {
        $text = [System.IO.File]::ReadAllText($manifestFile.FullName, [System.Text.Encoding]::UTF8)
        $manifest = ConvertFrom-JobJson -Text $text
    }
    catch {
        Write-AgentLog -LogFile $LogFile -Message (
            'folder {0}: manifest is not valid JSON, skipped' -f $Relative
        )
        return
    }
    if ($null -eq $manifest -or $manifest -isnot [System.Management.Automation.PSCustomObject]) {
        Write-AgentLog -LogFile $LogFile -Message (
            'folder {0}: manifest is not a JSON object, skipped' -f $Relative
        )
        return
    }
    $property = $manifest.PSObject.Properties['invoice_id']
    if ($null -eq $property -or -not (Test-IntegerId -Value $property.Value)) {
        Write-AgentLog -LogFile $LogFile -Message (
            'folder {0}: manifest invoice_id is not an integer, skipped' -f $Relative
        )
        return
    }
    return [long] $property.Value
}

function Get-InvoiceFolderIndex {
    <#
    .SYNOPSIS
        Index a tree as invoice id -> folder path relative to the root.
    .DESCRIPTION
        Two folders with the same id make that id ambiguous, so it is logged and
        skipped instead of guessing which copy is current.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $Root,
        [string] $LogFile = ''
    )

    $index = @{}
    $skipped = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
    foreach ($folder in @(Get-InvoiceFolderCandidates -Root $Root)) {
        $relative = Get-RelativeFolderPath -Root $Root -Path $folder.FullName
        $invoiceId = Read-InvoiceManifest -Folder $folder.FullName -Relative $relative -LogFile $LogFile
        if ($null -eq $invoiceId) {
            continue
        }
        $key = [string] $invoiceId
        if ($skipped.Contains($key)) {
            Write-AgentLog -LogFile $LogFile -Message (
                'invoice {0}: another folder {1}, id skipped' -f $key, $relative
            )
            continue
        }
        if ($index.ContainsKey($key)) {
            [void] $skipped.Add($key)
            $index.Remove($key)
            Write-AgentLog -LogFile $LogFile -Message (
                'invoice {0}: folder {1} duplicates another, id skipped' -f $key, $relative
            )
            continue
        }
        $index[$key] = $relative
    }
    return $index
}

function Sync-Folders {
    <#
    .SYNOPSIS
        Mirror the personal invoice folders into the work root by manifest id (R-7.3, R-7.14).
    .DESCRIPTION
        A folder missing on the work side is copied whole with its relative path.
        A folder whose relative path differs is moved to match the personal
        side, unless the target already exists: nothing is ever overwritten or
        deleted. Then every personal file missing on the work side, or with a
        different SHA-256 hash, is copied over. Folders without a manifest are
        left alone.
    .PARAMETER PersonalRoot
        The personal OneDrive root, the source of the mirror.
    .PARAMETER WorkRoot
        The work OneDrive root, the target of the mirror.
    .PARAMETER LogFile
        The log to append to; defaults to `<script folder>/agent.log`.
    .PARAMETER DryRun
        Log the planned copies and moves and touch nothing.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string] $PersonalRoot,
        [Parameter(Mandatory)]
        [string] $WorkRoot,
        [string] $LogFile = '',
        [switch] $DryRun
    )

    if ([string]::IsNullOrEmpty($LogFile)) {
        $LogFile = Join-Path -Path $PSScriptRoot -ChildPath 'agent.log'
    }
    $personalIndex = Get-InvoiceFolderIndex -Root $PersonalRoot -LogFile $LogFile
    $workIndex = Get-InvoiceFolderIndex -Root $WorkRoot -LogFile $LogFile

    foreach ($invoiceId in @($personalIndex.Keys)) {
        $personalRelative = [string] $personalIndex[$invoiceId]
        $personalPath = Join-RootPath -Root $PersonalRoot -Relative $personalRelative
        $workPath = $null
        $workRelative = $null
        if ($workIndex.ContainsKey($invoiceId)) {
            $workRelative = [string] $workIndex[$invoiceId]
            $workPath = Join-RootPath -Root $WorkRoot -Relative $workRelative
        }

        if ($null -eq $workPath) {
            $targetPath = Join-RootPath -Root $WorkRoot -Relative $personalRelative
            if (Test-Path -LiteralPath $targetPath) {
                Write-AgentLog -LogFile $LogFile -Message (
                    'invoice {0}: work path {1} already exists, skipped' -f $invoiceId, $personalRelative
                )
                continue
            }
            if ($DryRun) {
                Write-AgentLog -LogFile $LogFile -Message (
                    'would copy folder {0} (invoice {1})' -f $personalRelative, $invoiceId
                )
                continue
            }
            $targetParent = Split-Path -Path $targetPath -Parent
            if (-not (Test-Path -LiteralPath $targetParent -PathType Container)) {
                New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
            }
            Copy-Item -LiteralPath $personalPath -Destination $targetPath -Recurse
            Write-AgentLog -LogFile $LogFile -Message (
                'copied folder {0} (invoice {1})' -f $personalRelative, $invoiceId
            )
            continue
        }

        if (-not (Test-SameRelativePath -Left $workRelative -Right $personalRelative)) {
            $targetPath = Join-RootPath -Root $WorkRoot -Relative $personalRelative
            if (Test-Path -LiteralPath $targetPath) {
                Write-AgentLog -LogFile $LogFile -Message (
                    'invoice {0}: work path {1} already exists, skipped' -f $invoiceId, $personalRelative
                )
                continue
            }
            if ($DryRun) {
                Write-AgentLog -LogFile $LogFile -Message (
                    'would move folder {0} to {1} (invoice {2})' -f
                        $workRelative, $personalRelative, $invoiceId
                )
                continue
            }
            $targetParent = Split-Path -Path $targetPath -Parent
            if (-not (Test-Path -LiteralPath $targetParent -PathType Container)) {
                New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
            }
            Move-Item -LiteralPath $workPath -Destination $targetPath
            Write-AgentLog -LogFile $LogFile -Message (
                'moved folder {0} to {1} (invoice {2})' -f $workRelative, $personalRelative, $invoiceId
            )
            $workPath = $targetPath
        }

        $copiedFiles = 0
        foreach ($file in @(Get-ChildItem -LiteralPath $personalPath -File -Recurse -Force)) {
            $fileRelative = Get-RelativeFolderPath -Root $personalPath -Path $file.FullName
            $destination = Join-RootPath -Root $workPath -Relative $fileRelative
            $needsCopy = $true
            if (Test-Path -LiteralPath $destination -PathType Leaf) {
                $sourceHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
                $destinationHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash
                $needsCopy = $sourceHash -ne $destinationHash
            }
            if (-not $needsCopy) {
                continue
            }
            if (-not $DryRun) {
                $destinationParent = Split-Path -Path $destination -Parent
                if (-not (Test-Path -LiteralPath $destinationParent -PathType Container)) {
                    New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
                }
                Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
            }
            $copiedFiles++
        }
        if ($copiedFiles -gt 0) {
            $verb = if ($DryRun) { 'would copy' } else { 'copied' }
            Write-AgentLog -LogFile $LogFile -Message (
                '{0} {1} file(s) in {2} (invoice {3})' -f
                    $verb, $copiedFiles, $personalRelative, $invoiceId
            )
        }
    }
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
        $job = Test-JobFile -Path $jobFile.FullName -Key $agentKey -Allowlist $agentAllowlist `
            -Root $OneDriveRoot
        if ($null -ne $job) {
            Write-Verbose ("job {0} is valid" -f $jobFile.BaseName)
            $folder = Join-RootPath -Root $OneDriveRoot -Relative $job.folder
            $validJobs.Add([pscustomobject] @{
                    Job    = $job
                    Folder = $folder
                    Path   = $jobFile.FullName
                })
        }
    }

    $agentLog = Join-Path -Path $PSScriptRoot -ChildPath 'agent.log'
    if ($validJobs.Count -gt 0) {
        $stateFile = Join-Path -Path $PSScriptRoot -ChildPath 'sent-jobs.txt'
        $outlook = $null
        if (-not $DryRun) {
            $outlook = New-Object -ComObject Outlook.Application
        }
        foreach ($validJob in $validJobs) {
            Send-Job -Job $validJob.Job -Path $validJob.Path -Folder $validJob.Folder -Outlook $outlook `
                -StateFile $stateFile -Outbox $outbox -Key $agentKey -LogFile $agentLog -DryRun:$DryRun
        }
    }

    Sync-Folders -PersonalRoot $OneDriveRoot -WorkRoot $WorkRoot -LogFile $agentLog -DryRun:$DryRun
}
