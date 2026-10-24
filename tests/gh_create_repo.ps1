$ErrorActionPreference = "Stop"
Set-Location C:\Users\wang\Desktop\review-agent
$log = "tests\git_setup2.log"
"branch=$(git branch --show-current)" *>> $log
$cred = "url=https://github.com/W-zai1/review-agent" | git credential fill
$env:GH_TOKEN = ($cred | Where-Object { $_ -like "password=*" }) -replace "password=", ""
$body = @{
    name        = "review-agent"
    private     = $false
    description = "审查 Agent 系统：S1-S6 审查引擎（类型检测 + 规则层 + 确定性算术 + 可选 LLM 语义层 + 报告哈希链）"
} | ConvertTo-Json
try {
    $r = Invoke-RestMethod -Uri "https://api.github.com/user/repos" -Method Post `
        -Headers @{ Authorization = "Bearer $env:GH_TOKEN"; "User-Agent" = "review-agent-cli"; Accept = "application/vnd.github+json" } `
        -ContentType "application/json" -Body $body
    "created=$($r.full_name) url=$($r.html_url)" *>> $log
} catch {
    $status = $_.Exception.Response.StatusCode.value__
    $detail = $_.ErrorDetails.Message
    "create_failed=status:$status detail:$detail" *>> $log
}
