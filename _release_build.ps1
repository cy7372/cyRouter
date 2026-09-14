# 发布包装：修正 pi 子进程环境后执行 build.ps1
# - PATH 前置 miniconda 三件套（root/Scripts/Library\bin）——Library\bin 里有 conda 的
#   libssl/libcrypto/libexpat/ffi 等 DLL，缺了会导致 PyInstaller 打出的 exe 启动即崩
#   （2026-09-07 实测 pyexpat ImportError），必须让 PyInstaller 能解析到
# - LOCALAPPDATA 指回真实用户目录——pi 子进程继承的是 systemprofile 的值，
#   build.ps1 靠它找 Inno Setup（ISCC.exe），不修就静默跳过安装包
$env:Path = "C:\Users\CyYu\miniconda3;C:\Users\CyYu\miniconda3\Scripts;C:\Users\CyYu\miniconda3\Library\bin;" + $env:Path
$env:LOCALAPPDATA = "C:\Users\CyYu\AppData\Local"
Set-Location D:\Programs\cyRouter
& .\build.ps1
