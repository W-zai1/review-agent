$ErrorActionPreference = "Stop"
$log = "tests\git_token_check.log"
$cred = "url=https://github.com/W-zai1/review-agent" | git credential fill
$env:GH_TOKEN = ($cred | Where-Object { $_ -like "password=*" }) -replace "password=", ""
"token_len=$($env:GH_TOKEN.Length) prefix=$($env:GH_TOKEN.Substring(0,4))" *>> $log
try {
    $r = Invoke-RestMethod -Uri "https://api.github.com/user" -Method Get `
        -Headers @{ Authorization = "Bearer $env:GH_TOKEN"; "User-Agent" = "review-agent-cli" } -ErrorAction Stop
    "user_ok=login:$($r.login) id:$($r.id)" *>> $log
} catch {
    $status = $_.Exception.Response.StatusCode.value__
    $detail = $_.ErrorDetails.Message
    "user_failed=status:$status detail:$detail" *>> $log
}
# 也试 token 格式（旧式）
try {
    $r2 = Invoke-RestMethod -Uri "https://api.github.com/user" -Method Get `
        -Headers @{ Authorization = "token $env:GH_TOKEN"; "User-Agent" = "review-agent-cli" } -ErrorAction Stop
    "user_ok_tokenfmt=login:$($r2.login)" *>> $log
} catch {
    $status = $_.Exception.Response.StatusCode.value__
    "user_failed_tokenfmt=status:$status" *>> $log
}
