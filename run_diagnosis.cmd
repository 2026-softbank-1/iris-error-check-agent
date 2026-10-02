@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found. Follow README installation steps.
  popd
  exit /b 1
)
if "%~1"=="" (
  ".venv\Scripts\python.exe" -m ai_error_check_agent diagnose --request "examples\configuration.request.json"
) else (
  ".venv\Scripts\python.exe" -m ai_error_check_agent diagnose %*
)
set "IRIS_DIAGNOSIS_EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %IRIS_DIAGNOSIS_EXIT_CODE%
