@echo off
:: Change directory to the script's location
cd /d "%~dp0"

(
 echo;
 echo %DATE% %TIME% Processing file: %1
 echo ---- START ----
 python rocket-tweaker.py %1
 echo ----- END -----
 echo;
) >> rocket-tweaker.log 2>&1