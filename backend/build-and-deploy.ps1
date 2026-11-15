# SensorWatch - builds the Lambda zips from backend/, uploads them to S3 and updates the functions.
# Run from the backend folder:
#   .\build-and-deploy.ps1                      (all functions)
#   .\build-and-deploy.ps1 -Only ingest,analyze (only these folders)
param(
  [string]   $Bucket  = "sensorwatch-code-992382830690",
  [string]   $Region  = "us-east-1",
  [string]   $Project = "SensorWatch",
  [string[]] $Only
)

$root  = $PSScriptRoot
$build = Join-Path $root "build"

# folder name -> Lambda / zip name
$map = [ordered]@{
  "authorizer"       = "authorizer"
  "ingest"           = "ingest"
  "devices_query"    = "devices-query"
  "analyze"          = "analyze"
  "offline_check"    = "offline-check"
  "aggregate_export" = "aggregate-export"
}

if (Test-Path $build) { Remove-Item -Recurse -Force $build }
New-Item -ItemType Directory $build | Out-Null

foreach ($dir in $map.Keys) {
  if ($Only -and ($Only -notcontains $dir)) { continue }
  $name = $map[$dir]
  $src  = Join-Path $root $dir
  if (-not (Test-Path (Join-Path $src "app.py"))) { Write-Warning "Skipping $dir (no app.py)"; continue }

  Write-Host "`n=== $name ===" -ForegroundColor Cyan
  $stage = Join-Path $build $name
  New-Item -ItemType Directory $stage | Out-Null
  Copy-Item "$src\*" $stage -Recurse
  if (Test-Path (Join-Path $root "common")) { Copy-Item (Join-Path $root "common") (Join-Path $stage "common") -Recurse }
  Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

  $zip = Join-Path $build "$name.zip"
  Compress-Archive -Path "$stage\*" -DestinationPath $zip -Force

  aws s3 cp $zip "s3://$Bucket/lambda/$name.zip" --region $Region
  if ($LASTEXITCODE -ne 0) { throw "Upload of $name.zip failed" }

  aws lambda update-function-code --function-name "$Project-$name" --s3-bucket $Bucket `
      --s3-key "lambda/$name.zip" --region $Region --query "LastModified" --output text
  if ($LASTEXITCODE -ne 0) { throw "Update of $Project-$name failed" }
  aws lambda wait function-updated --function-name "$Project-$name" --region $Region
}
Write-Host "`nDone." -ForegroundColor Green
