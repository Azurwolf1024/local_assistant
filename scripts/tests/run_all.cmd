@echo off
rem Run the offline test suite (no mic, no speakers needed) and print a summary.
rem ASCII ONLY in this file: cmd.exe reads .cmd as the console codepage (GBK),
rem so any non-ASCII comment becomes a bogus command (hit that twice already).
rem Delayed expansion is required: %ERRORLEVEL% inside a for block is expanded
rem at parse time, which would print the pre-loop value (always 0) for every line.
setlocal enabledelayedexpansion
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
rem This file lives in scripts\tests\ -- work from the project root it belongs to.
cd /d "%~dp0..\.."
rem Preflight: a full suite loads Whisper / SenseVoice / ZipVoice / Ollama at once.
rem With a Piper training running on this 31.5 GB box that starves RAM, and the
rem training dies on the next checkpoint save with 0xC0000005 (17 crashes so far).
rem So: refuse instead of risking it. Override with: preflight_heavy.py --force
python scripts\preflight_heavy.py
if errorlevel 1 (
  echo PREFLIGHT_REFUSED - suite not started ^(reason above; see scripts\preflight_heavy.py^)
  exit /b 1
)
if not exist sessions\testlogs mkdir sessions\testlogs
for %%T in (test_offline test_events test_event_text test_precision test_refclean test_pitch test_textcheck test_voice_model test_trim_pacing test_names test_wake_apply test_phonetic test_persona test_persona_guard test_import_lines test_persona_card test_prepare_dataset test_prepare_segments test_train_watch test_train_forever test_memory test_memory_dream test_memory_mcp test_memory_sync test_agent_tools test_tts_registry test_asr_registry test_module_boundaries test_weather test_mail test_tray test_vision test_voice_clone test_speaker test_style test_route test_tools test_mcp test_accel test_skills_route test_hotkey test_subtitle_sync test_store_lock test_control test_service_ctl test_console test_console_exit test_setup_flow test_update test_paths test_persona_import) do (
  python scripts\tests\%%T.py > sessions\testlogs\%%T.log 2>&1
  echo %%T EXIT=!ERRORLEVEL!
)
rem lint_persona: not a unit test, it lints the REAL persona files (exits 1 on warnings).
rem It belongs in the suite because every prompt change must keep those files clean.
python scripts\lint_persona.py > sessions\testlogs\lint_persona.log 2>&1
echo lint_persona EXIT=!ERRORLEVEL!
echo ALLDONE
