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
