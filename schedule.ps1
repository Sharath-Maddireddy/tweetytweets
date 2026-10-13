# Registers every tweetytweets job on this Windows machine (run from the repo folder).
# Each job goes through scripts\run.py, which checks sign-ins first and alerts Telegram
# on any failure. Re-run this file after changing times. Times are local (set Windows to IST).
$py = "$PWD\.venv\Scripts\python.exe"
function Job($name, $arg, $triggers) {
  $a = New-ScheduledTaskAction -Execute $py -Argument "scripts\run.py $arg" -WorkingDirectory $PWD
  Register-ScheduledTask -TaskName "tweetytweets $name" -Action $a -Trigger $triggers -Force | Out-Null
  Write-Host "registered: $name"
}
# remove the older cmd.exe-based tasks so nothing runs twice
"tweetytweets replies","tweetytweets refresh accounts","tweetytweets daily post" | ForEach-Object {
  Unregister-ScheduledTask -TaskName $_ -Confirm:$false -ErrorAction SilentlyContinue }

Job "replies"    "replies"    ("08:30","20:25","23:30" | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ })
Job "daily post" "daily_post" (New-ScheduledTaskTrigger -Daily -At 20:15)
Job "li watch"   "li_watch"   (New-ScheduledTaskTrigger -Daily -At 10:30)
Job "leads"      "leads"      (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 10:00)
Job "refresh"    "refresh"    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 10:00)
Job "heartbeat"  "heartbeat"  (New-ScheduledTaskTrigger -Daily -At 22:45)

# the Telegram bot: starts at logon, restarts if it ever dies
$a = New-ScheduledTaskAction -Execute $py -Argument "scripts\bot.py" -WorkingDirectory $PWD
$s = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "tweetytweets bot" -Action $a -Trigger (New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME") -Settings $s -Force | Out-Null
Start-ScheduledTask -TaskName "tweetytweets bot"
Write-Host "registered + started: bot"
