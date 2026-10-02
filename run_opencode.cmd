@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment not found. See README.md.
  popd
  exit /b 1
)
".venv\Scripts\python.exe" -m ai_error_check_agent.interactive %*
set "agent_exit=%errorlevel%"
popd
exit /b %agent_exit%
