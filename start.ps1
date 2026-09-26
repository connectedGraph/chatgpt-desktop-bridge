[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  ChatGPT Desktop Bridge (CDP / OpenAI API Adapter)" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

# Ensure dependencies
python -m pip install -r "$PSScriptRoot\requirements.txt" -q

# Run bridge
python "$PSScriptRoot\bridge.py" @args
