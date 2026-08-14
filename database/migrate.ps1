<#
.SYNOPSIS
    Applies the salesops analytics migrations to the running PostgreSQL container.

.DESCRIPTION
    Runs every database/migrations/V*.sql in filename order via psql, stopping at
    the first error. Migrations are idempotent, so re-running is safe and is the
    normal way to bring an existing database up to date.

    Files are read from inside the container (./database is mounted at /database)
    rather than piped through the shell, so encoding and line endings cannot be
    altered in transit.

.PARAMETER Test
    After migrating, run the schema validation suite.

.EXAMPLE
    .\database\migrate.ps1
    .\database\migrate.ps1 -Test
#>
[CmdletBinding()]
param(
    [string] $Database = 'salesops',
    [string] $User     = 'salesops',
    [switch] $Test
)

$ErrorActionPreference = 'Stop'
$repoRoot    = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'docker-compose.yml'

# Windows PowerShell 5.1 wraps a native command's stderr in ErrorRecords, and
# under $ErrorActionPreference = 'Stop' that makes any stderr output - including
# a harmless docker warning - look like a fatal error. Native calls therefore run
# with 'Continue' and are judged on $LASTEXITCODE, which is the actual signal.
function Invoke-Native {
    param([Parameter(Mandatory)] [scriptblock] $Command)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command } finally { $ErrorActionPreference = $previous }
}

function Invoke-PsqlFile {
    param([Parameter(Mandatory)] [string] $ContainerPath)

    Invoke-Native {
        docker compose -f $composeFile exec -T postgres `
            psql -U $User -d $Database -v ON_ERROR_STOP=1 --quiet -f $ContainerPath
    }

    if ($LASTEXITCODE -ne 0) {
        throw "psql failed on $ContainerPath (exit code $LASTEXITCODE)"
    }
}

$migrations = Get-ChildItem -Path (Join-Path $PSScriptRoot 'migrations') -Filter 'V*.sql' |
              Sort-Object Name

if (-not $migrations) { throw "No migrations found in $PSScriptRoot\migrations" }

Write-Host "Applying $($migrations.Count) migration(s) to '$Database'..." -ForegroundColor Cyan

foreach ($migration in $migrations) {
    Write-Host "  -> $($migration.Name)"
    Invoke-PsqlFile "/database/migrations/$($migration.Name)"
}

Write-Host "Migrations applied." -ForegroundColor Green

Invoke-Native {
    docker compose -f $composeFile exec -T postgres `
        psql -U $User -d $Database --quiet `
        -c "SELECT version, description, applied_at FROM salesops.schema_migrations ORDER BY version;"
}

if ($Test) {
    Write-Host "`nRunning schema validation..." -ForegroundColor Cyan
    Invoke-PsqlFile '/database/tests/test_analytics_schema.sql'
    Write-Host "Schema validation passed." -ForegroundColor Green
}
