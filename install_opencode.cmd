@echo off
setlocal
pushd "%~dp0"
call npm.cmd install --prefix .runtime/opencode-tooling --save-exact --no-audit --no-fund opencode-ai@1.18.34
set "agent_install_exit=%errorlevel%"
popd
exit /b %agent_install_exit%
