<#
  daily.ps1 - 평일 하루 세 번 도는 데일리 배치 (Windows 작업 스케줄러용)

    08:00  개장 전 평가 갱신. 전 거래일은 이미 처리됐으므로 매매는 일어나지 않고,
           확정 종가로 평가손익과 손절선 근접 여부만 다시 계산한다.
    11:30  장중 모니터링(-Monitor). 지금 시세로 평가만 갱신한다. 매매는 기록하지 않는다 —
           장중 가격으로 체결을 남기면 마감까지 가격이 움직여도 되돌릴 수 없다.
    15:05  주문 구간. 그 시점 스냅샷을 종가 대용으로 신호를 판정하고 체결까지 기록한다.
           이걸 보고 마감 동시호가(15:20~15:30)에 주문한다.

  왜 PC 에서 도는가:
    GitHub Actions 의 예약 실행은 best-effort 라 수십 분~수 시간 밀리거나 건너뛴다.
    실제로 9/10 은 5시간 지연(20:10), 9/11 은 아예 실행되지 않았다.
    이 전략은 마감(15:30) 직전 신호를 보고 동시호가에 주문해야 하므로 시간 정확도가
    곧 기능이다. 그래서 정시성이 보장되는 로컬 스케줄러를 주 경로로 쓴다.

  하는 일: git pull -> run_daily.py -> 결과가 바뀌었으면 commit & push
  로그: run.log (gitignore 됨)
#>

param([switch]$Monitor)      # -Monitor: 장중 모니터링 (평가만 갱신, 매매 기록 없음)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root
$log = Join-Path $root "run.log"

function Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Output $line
    Add-Content -LiteralPath $log -Value $line -Encoding utf8
}

$py = "C:\Users\Check\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

# python 은 UTF-8 로 출력하는데(PYTHONIOENCODING) PowerShell 5.1 은 네이티브 명령
# 출력을 OEM 코드페이지(한국어 Windows = cp949)로 디코딩한다. 맞춰주지 않으면
# run.log 의 한글이 전부 깨져서, 정작 실패를 조사할 때 읽을 수가 없다.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Log ("===== daily.ps1 시작" + $(if ($Monitor) { " (장중 모니터링)" } else { "" }) + " =====")

# 1) 원격 변경분 먼저 받기 (Actions 백업 실행과 충돌 방지)
git pull --rebase --autostash 2>&1 | ForEach-Object { Log "  git pull | $_" }

# 2) 배치 실행. 15:00 이전이면 run_daily 가 스스로 거부한다.
$env:PYTHONIOENCODING = "utf-8"
$pyArgs = @("run_daily.py")
if ($Monitor) { $pyArgs += "--monitor" }
& $py $pyArgs 2>&1 | ForEach-Object { Log "  $_" }
$code = $LASTEXITCODE
if ($code -ne 0) {
    Log "run_daily.py 실패 (exit $code) - 커밋하지 않고 종료"
    exit $code
}

# 3) 결과가 실제로 바뀐 경우에만 커밋
git add portfolio_state.json snapshot.json 2>&1 | Out-Null
git diff --staged --quiet
if ($LASTEXITCODE -eq 0) {
    Log "변경 없음 - 커밋 생략"
    Log "===== 완료 ====="
    exit 0
}

$msg = "daily update {0}" -f (Get-Date -Format "yyyy-MM-dd")
git commit -m $msg 2>&1 | ForEach-Object { Log "  git commit | $_" }
git push 2>&1 | ForEach-Object { Log "  git push | $_" }
if ($LASTEXITCODE -ne 0) { Log "push 실패 - 다음 실행에서 재시도됨"; exit 1 }

Log "===== 완료 ====="
