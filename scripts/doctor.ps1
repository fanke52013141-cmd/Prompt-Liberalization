# 诊断脚本：环境/数据/一致性检查（对应 PRD P14 doctor 能力）
$root = Split-Path -Parent $PSScriptRoot
Write-Host "== 提示词优化实验室 诊断 ==" -ForegroundColor Cyan

$ver = (& python --version) 2>&1
Write-Host "Python        : $ver"
$ok = & python -c "import fastapi, uvicorn; print('fastapi/uvicorn OK')" 2>$null
if ($ok) { Write-Host "依赖          : $ok" } else { Write-Host "依赖          : 缺少 fastapi/uvicorn，请运行 pip install -r requirements.txt" -ForegroundColor Yellow }

$db = Join-Path $root "data\prompt_lab.db"
if (Test-Path $db) {
    Write-Host "数据库        : $db ($([math]::Round((Get-Item $db).Length/1KB,1)) KB)"
    & python -c @"
import sqlite3, sys
conn = sqlite3.connect(r'$db')
counts = {}
for t in ('projects','dataset_items','outputs','runs','ledger','acceptance_reports','releases','feedback'):
    counts[t] = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
for k, v in counts.items():
    print(f'  {k:20s} {v}')
bad = conn.execute('SELECT COUNT(*) FROM ledger WHERE status=''reserved''').fetchone()[0]
print(f'  在途预留(应随运行结束归零或保留为sent_unknown): {bad}')
"@
} else {
    Write-Host "数据库        : 尚未创建（首次启动后生成）" -ForegroundColor Yellow
}
Write-Host "浏览器访问    : http://127.0.0.1:8620 （需先 start.ps1）"
