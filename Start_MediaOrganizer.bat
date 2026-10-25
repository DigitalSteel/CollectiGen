@echo off
setlocal DisableDelayedExpansion
set "ORGANIZER_ROOT=%~dp0"
set "ORGANIZER_PROFILE="
if "%~1"=="" (
    if not "%~2"=="" goto organizer_usage
    goto organizer_prerequisites
)
if /i not "%~1"=="--profile-root" goto organizer_usage
if "%~2"=="" goto organizer_usage
if not "%~3"=="" goto organizer_usage
set "ORGANIZER_PROFILE=%~f2"

:organizer_prerequisites
cd /d "%ORGANIZER_ROOT%."
if errorlevel 1 goto organizer_missing_files
if not exist "%ORGANIZER_ROOT%tools\OrganizerPrerequisites.exe" goto organizer_checker_failed
if not exist "%ORGANIZER_ROOT%Runtime\python\python.exe" goto organizer_missing_files
"%ORGANIZER_ROOT%tools\OrganizerPrerequisites.exe"
set "ORGANIZER_CHECK_EXIT=%ERRORLEVEL%"
if not "%ORGANIZER_CHECK_EXIT%"=="0" goto organizer_checker_failed
if defined ORGANIZER_PROFILE goto organizer_explicit_profile
"%ORGANIZER_ROOT%Runtime\python\python.exe" -B -m suite_core.media_organizer --runtime-profile organizer-image --runtime-root "%ORGANIZER_ROOT%Runtime"
set "ORGANIZER_EXIT=%ERRORLEVEL%"
goto organizer_finished

:organizer_explicit_profile
"%ORGANIZER_ROOT%Runtime\python\python.exe" -B -m suite_core.media_organizer --runtime-profile organizer-image --runtime-root "%ORGANIZER_ROOT%Runtime" --profile-root "%ORGANIZER_PROFILE%\."
set "ORGANIZER_EXIT=%ERRORLEVEL%"

:organizer_finished
if not "%ORGANIZER_EXIT%"=="0" (
    echo Media Organizer did not complete normally. Keep the diagnostic above when reporting the problem.
    echo Do not delete your profile, installed files or caches to retry.
    pause
)
endlocal & exit /b %ORGANIZER_EXIT%

:organizer_missing_files
echo Media Organizer could not find its complete extracted launcher/runtime files.
echo Extract the whole ZIP into a writable folder. Do not use system Python as a fallback.
pause
exit /b 1

:organizer_usage
echo Usage: Start_MediaOrganizer.bat [--profile-root "C:\Your chosen test profile"]
echo Only --profile-root is accepted. The bundled runtime selection stays fixed.
exit /b 2

:organizer_checker_failed
echo Media Organizer could not verify its Microsoft Visual C++ x64 prerequisite.
echo The checker may be missing, damaged, blocked by policy, or reporting an unavailable prerequisite.
echo Read WINDOWS_PREREQUISITES.md for official installation instructions and policy limits.
echo No installation, repair or security-policy change was attempted.
pause
exit /b 1
