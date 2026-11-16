BeforeAll {
    $script:Here = $PSScriptRoot
    . (Join-Path -Path $script:Here -ChildPath '..' '..' 'scripts' 'laptop_agent.ps1')

    $script:Vectors = Get-Content -LiteralPath (Join-Path -Path $script:Here -ChildPath 'vectors.json') -Raw |
        ConvertFrom-Json
    $script:Allowlist = @('hr@example.com')

    function script:New-InvoiceFolder {
        param(
            [string] $Root,
            [string] $RelativeFolder,
            [string[]] $Files = @()
        )
        $path = $Root
        foreach ($segment in $RelativeFolder.Split('/')) {
            $path = Join-Path -Path $path -ChildPath $segment
        }
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        foreach ($file in $Files) {
            [System.IO.File]::WriteAllText((Join-Path -Path $path -ChildPath $file), 'x')
        }
        return $path
    }

    function script:Write-JobJson {
        param(
            [string] $Path,
            $Job
        )
        $json = $Job | ConvertTo-Json -Depth 5
        [System.IO.File]::WriteAllText($Path, $json, [System.Text.UTF8Encoding]::new($false))
    }

    function script:Copy-Job {
        param($Job)
        $copy = [ordered] @{}
        foreach ($property in $Job.PSObject.Properties) {
            $copy[$property.Name] = $property.Value
        }
        return [pscustomobject] $copy
    }

    function script:New-SignedJob {
        param([System.Collections.IDictionary] $Fields)
        $hmac = Get-JobSignature -Job ([pscustomobject] $Fields) -Key $script:Vectors.key
        $signed = [ordered] @{}
        foreach ($name in $Fields.Keys) {
            $signed[$name] = $Fields[$name]
        }
        $signed['hmac'] = $hmac
        return [pscustomobject] $signed
    }

    function script:New-FakeOutlook {
        param([switch] $ThrowOnSend)
        $outlook = [pscustomobject] @{
            CreateItemCalls = [System.Collections.Generic.List[int]]::new()
            SentItems       = [System.Collections.Generic.List[object]]::new()
            ThrowOnSend     = [bool] $ThrowOnSend
        }
        $outlook | Add-Member -MemberType ScriptMethod -Name CreateItem -Value {
            param($itemType)
            [void] $this.CreateItemCalls.Add([int] $itemType)
            $attachments = [pscustomobject] @{
                Paths = [System.Collections.Generic.List[string]]::new()
            }
            $attachments | Add-Member -MemberType ScriptMethod -Name Add -Value {
                param($path)
                $this.Paths.Add([string] $path)
            }
            $item = [pscustomobject] @{
                To          = ''
                Subject     = ''
                Body        = ''
                Attachments = $attachments
                Sent        = $false
                ThrowOnSend = $this.ThrowOnSend
            }
            $item | Add-Member -MemberType ScriptMethod -Name Send -Value {
                if ($this.ThrowOnSend) {
                    throw 'outlook refused to send'
                }
                $this.Sent = $true
            }
            $this.SentItems.Add($item)
            return $item
        }
        return $outlook
    }

    function script:New-SendFixture {
        param(
            [string[]] $Attachments = @('F001-123.pdf'),
            [string] $Body = 'Hola, adjunto el reembolso.'
        )
        $job = New-SignedJob -Fields ([ordered] @{
                job_id      = '0123456789abcdef0123456789abcdef'
                invoice_id  = 1
                folder      = 'Reembolsos/2026-11/F001-123'
                to          = @('hr@example.com')
                subject     = 'Support release 0001'
                body        = $Body
                attachments = $Attachments
            })
        $runId = [guid]::NewGuid().ToString('N')
        $root = Join-Path -Path $TestDrive -ChildPath ('onedrive-' + $runId)
        $folder = New-InvoiceFolder -Root $root -RelativeFolder $job.folder -Files @($Attachments)
        $outbox = Join-Path -Path $root -ChildPath '_outbox'
        New-Item -ItemType Directory -Path $outbox -Force | Out-Null
        $jobFile = Join-Path -Path $outbox -ChildPath ($job.job_id + '.json')
        Write-JobJson -Path $jobFile -Job $job
        return [pscustomobject] @{
            Job       = $job
            Folder    = $folder
            Outbox    = $outbox
            JobFile   = $jobFile
            StateFile = Join-Path -Path $TestDrive -ChildPath ('sent-jobs-' + $runId + '.txt')
            LogFile   = Join-Path -Path $TestDrive -ChildPath ('agent-' + $runId + '.log')
        }
    }
}

Describe 'Test-JobFile' {
    It 'R-7.14 verifies both job vectors' {
        foreach ($job in $script:Vectors.jobs) {
            $root = Join-Path -Path $TestDrive -ChildPath ([guid]::NewGuid().ToString('N'))
            $folder = New-InvoiceFolder -Root $root -RelativeFolder $job.folder -Files @($job.attachments)
            $path = Join-Path -Path $root -ChildPath ($job.job_id + '.json')
            Write-JobJson -Path $path -Job $job

            Test-JobFile -Path $path -Key $script:Vectors.key -Allowlist $script:Allowlist -Folder $folder |
                Should -BeTrue
        }
    }

    It 'R-7.14 refuses a job with one changed byte' {
        $job = Copy-Job -Job $script:Vectors.jobs[0]
        $job.body = $job.body + '!'
        $root = Join-Path -Path $TestDrive -ChildPath ([guid]::NewGuid().ToString('N'))
        $folder = New-InvoiceFolder -Root $root -RelativeFolder $job.folder -Files @($job.attachments)
        $path = Join-Path -Path $root -ChildPath ($job.job_id + '.json')
        Write-JobJson -Path $path -Job $job

        $warnings = @()
        Test-JobFile -Path $path -Key $script:Vectors.key -Allowlist $script:Allowlist -Folder $folder `
            -WarningAction SilentlyContinue -WarningVariable warnings | Should -BeFalse
        [string] $warnings[0] | Should -Match 'hmac does not verify'
    }

    It 'R-7.14 refuses a recipient that is not on the allowlist' {
        $job = $script:Vectors.jobs[0]
        $root = Join-Path -Path $TestDrive -ChildPath ([guid]::NewGuid().ToString('N'))
        $folder = New-InvoiceFolder -Root $root -RelativeFolder $job.folder -Files @($job.attachments)
        $path = Join-Path -Path $root -ChildPath ($job.job_id + '.json')
        Write-JobJson -Path $path -Job $job

        $warnings = @()
        Test-JobFile -Path $path -Key $script:Vectors.key -Allowlist @('other@example.com') -Folder $folder `
            -WarningAction SilentlyContinue -WarningVariable warnings | Should -BeFalse
        [string] $warnings[0] | Should -Match 'recipient is not allowlisted'
    }

    It 'R-7.14 refuses an attachment that leaves the invoice folder' {
        foreach ($name in @('..\x.pdf', '../x.pdf')) {
            $job = New-SignedJob -Fields ([ordered] @{
                    job_id      = $script:Vectors.jobs[0].job_id
                    invoice_id  = 1
                    folder      = $script:Vectors.jobs[0].folder
                    to          = @('hr@example.com')
                    subject     = 'Support release 0001'
                    body        = 'Hola.'
                    attachments = @($name)
                })
            $root = Join-Path -Path $TestDrive -ChildPath ([guid]::NewGuid().ToString('N'))
            $folder = New-InvoiceFolder -Root $root -RelativeFolder $job.folder -Files @()
            [System.IO.File]::WriteAllText((Join-Path -Path $folder -ChildPath 'x.pdf'), 'x')
            $path = Join-Path -Path $root -ChildPath ($job.job_id + '.json')
            Write-JobJson -Path $path -Job $job

            $warnings = @()
            Test-JobFile -Path $path -Key $script:Vectors.key -Allowlist $script:Allowlist -Folder $folder `
                -WarningAction SilentlyContinue -WarningVariable warnings | Should -BeFalse
            [string] $warnings[0] | Should -Match 'attachment is not a plain file name'
        }
    }

    It 'R-7.14 refuses an attachment missing from the invoice folder' {
        $job = New-SignedJob -Fields ([ordered] @{
                job_id      = $script:Vectors.jobs[0].job_id
                invoice_id  = 1
                folder      = $script:Vectors.jobs[0].folder
                to          = @('hr@example.com')
                subject     = 'Support release 0001'
                body        = 'Hola.'
                attachments = @('missing.pdf')
            })
        $root = Join-Path -Path $TestDrive -ChildPath ([guid]::NewGuid().ToString('N'))
        $folder = New-InvoiceFolder -Root $root -RelativeFolder $job.folder -Files @()
        $path = Join-Path -Path $root -ChildPath ($job.job_id + '.json')
        Write-JobJson -Path $path -Job $job

        $warnings = @()
        Test-JobFile -Path $path -Key $script:Vectors.key -Allowlist $script:Allowlist -Folder $folder `
            -WarningAction SilentlyContinue -WarningVariable warnings | Should -BeFalse
        [string] $warnings[0] | Should -Match 'attachment is not in the invoice folder'
    }

    It 'R-7.14 refuses a 70 KiB job file' {
        $path = Join-Path -Path $TestDrive -ChildPath 'large.json'
        $content = '{"job_id":"' + ('a' * 32) + '"}'
        [System.IO.File]::WriteAllText($path, $content + ('x' * (70 * 1024)))

        $warnings = @()
        Test-JobFile -Path $path -Key $script:Vectors.key -Allowlist $script:Allowlist -Folder $TestDrive `
            -WarningAction SilentlyContinue -WarningVariable warnings | Should -BeFalse
        [string] $warnings[0] | Should -Match 'job file is too large'
    }
}

Describe 'Send-Job' {
    It 'R-7.14 sends a valid job once with three attachments and writes a sent receipt' {
        $attachments = @('Formato de reembolsos.xlsx', 'F001-123.pdf', 'F001-123-2.pdf')
        $fixture = New-SendFixture -Attachments $attachments
        $outlook = New-FakeOutlook

        Send-Job -Job $fixture.Job -Folder $fixture.Folder -Outlook $outlook `
            -StateFile $fixture.StateFile -Outbox $fixture.Outbox -LogFile $fixture.LogFile

        $outlook.CreateItemCalls.Count | Should -Be 1
        $outlook.SentItems.Count | Should -Be 1
        $item = $outlook.SentItems[0]
        $item.Sent | Should -BeTrue
        $item.To | Should -Be 'hr@example.com'
        $item.Subject | Should -Be 'Support release 0001'
        $item.Body | Should -Be $fixture.Job.body
        $item.Attachments.Paths.Count | Should -Be 3
        foreach ($name in $attachments) {
            $expected = Join-Path -Path $fixture.Folder -ChildPath $name
            $item.Attachments.Paths | Should -Contain $expected
        }

        $receiptPath = Join-Path -Path (Join-Path -Path $fixture.Outbox -ChildPath 'receipts') `
            -ChildPath ($fixture.Job.job_id + '.json')
        Test-Path -LiteralPath $receiptPath -PathType Leaf | Should -BeTrue
        $receipt = ConvertFrom-JobJson -Text (Get-Content -LiteralPath $receiptPath -Raw)
        ($receipt.PSObject.Properties.Name -join ',') | Should -Be 'job_id,sent_at'
        $receipt.job_id | Should -Be $fixture.Job.job_id
        { [DateTimeOffset]::Parse($receipt.sent_at) } | Should -Not -Throw
        $receipt.sent_at | Should -Match 'Z$'
        Test-Path -LiteralPath $fixture.JobFile | Should -BeFalse
        Get-Content -LiteralPath $fixture.StateFile | Should -Contain $fixture.Job.job_id
    }

    It 'R-7.14 running twice sends only once' {
        $fixture = New-SendFixture
        $outlook = New-FakeOutlook

        Send-Job -Job $fixture.Job -Folder $fixture.Folder -Outlook $outlook `
            -StateFile $fixture.StateFile -Outbox $fixture.Outbox -LogFile $fixture.LogFile
        Send-Job -Job $fixture.Job -Folder $fixture.Folder -Outlook $outlook `
            -StateFile $fixture.StateFile -Outbox $fixture.Outbox -LogFile $fixture.LogFile

        $outlook.CreateItemCalls.Count | Should -Be 1
        $outlook.SentItems.Count | Should -Be 1
        @(Get-Content -LiteralPath $fixture.StateFile | Where-Object { $_ -eq $fixture.Job.job_id }).Count |
            Should -Be 1
        $receiptPath = Join-Path -Path (Join-Path -Path $fixture.Outbox -ChildPath 'receipts') `
            -ChildPath ($fixture.Job.job_id + '.json')
        Test-Path -LiteralPath $receiptPath -PathType Leaf | Should -BeTrue
    }

    It 'R-7.14 a Send that throws writes an error receipt and drops the job id' {
        $fixture = New-SendFixture
        $outlook = New-FakeOutlook -ThrowOnSend

        Send-Job -Job $fixture.Job -Folder $fixture.Folder -Outlook $outlook `
            -StateFile $fixture.StateFile -Outbox $fixture.Outbox -LogFile $fixture.LogFile

        $receiptPath = Join-Path -Path (Join-Path -Path $fixture.Outbox -ChildPath 'receipts') `
            -ChildPath ($fixture.Job.job_id + '.json')
        Test-Path -LiteralPath $receiptPath -PathType Leaf | Should -BeTrue
        $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
        ($receipt.PSObject.Properties.Name -join ',') | Should -Be 'job_id,error'
        $receipt.job_id | Should -Be $fixture.Job.job_id
        $receipt.error | Should -BeOfType [string]
        $receipt.error.Length | Should -BeLessOrEqual 500
        $receipt.error | Should -Match 'outlook refused to send'
        @(Get-Content -LiteralPath $fixture.StateFile -ErrorAction SilentlyContinue |
                Where-Object { $_.Trim() -eq $fixture.Job.job_id }).Count | Should -Be 0
        Test-Path -LiteralPath $fixture.JobFile | Should -BeFalse
    }

    It 'R-7.14 -DryRun sends nothing and touches nothing but the log' {
        $fixture = New-SendFixture
        $outlook = New-FakeOutlook

        Send-Job -Job $fixture.Job -Folder $fixture.Folder -Outlook $outlook `
            -StateFile $fixture.StateFile -Outbox $fixture.Outbox -LogFile $fixture.LogFile -DryRun

        $outlook.CreateItemCalls.Count | Should -Be 0
        Test-Path -LiteralPath $fixture.JobFile -PathType Leaf | Should -BeTrue
        Test-Path -LiteralPath $fixture.StateFile | Should -BeFalse
        Test-Path -LiteralPath (Join-Path -Path $fixture.Outbox -ChildPath 'receipts') | Should -BeFalse
        Get-Content -LiteralPath $fixture.LogFile -Raw |
            Should -Match ('would send job ' + $fixture.Job.job_id + ' with 1 attachments')
    }

    It 'R-7.14 the log file never contains the body text' {
        $fixture = New-SendFixture -Body 'cuerpo secreto 4242'
        $outlook = New-FakeOutlook

        Send-Job -Job $fixture.Job -Folder $fixture.Folder -Outlook $outlook `
            -StateFile $fixture.StateFile -Outbox $fixture.Outbox -LogFile $fixture.LogFile

        $log = Get-Content -LiteralPath $fixture.LogFile -Raw
        $log | Should -Not -Match 'cuerpo secreto 4242'
        $log | Should -Match $fixture.Job.job_id
        $log | Should -Match 'sent'
    }
}
