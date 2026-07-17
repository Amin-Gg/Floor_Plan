param([string]$Output = "release/local/stage1-shards")
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
& python (Join-Path $Root "scripts/run_stage1_test_matrix.py") $Output
exit $LASTEXITCODE
