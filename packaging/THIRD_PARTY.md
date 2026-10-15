# 第三方元件與授權

VideoManager 的著作權聲明見專案根目錄的 `LICENSE.md`。以下元件有各自的授權；隨安裝檔附上的授權文字位於安裝目錄的 `_internal/third_party/licenses`，本檔也會一同安裝。

| 元件 | 安裝版本與用途 | 授權及來源 |
|---|---|---|
| Python | 3.12，程式執行環境 | Python Software Foundation License；[來源](https://www.python.org/downloads/source/) |
| PySide6 / Qt / Shiboken6 | 6.12.0，視窗與內建播放器 | LGPLv3 / GPLv3 / 商業授權選項；本包採 LGPLv3，見 [Qt for Python](https://doc.qt.io/qtforpython-6/) 與 [Qt 原始碼](https://code.qt.io/cgit/qt/) |
| FFmpeg / FFprobe | `n9.0.2-27-g7e93014c5b`，探測資訊與擷取預覽影格 | BtbN 的 Windows 64 位元 LGPLv3 shared 組建；[組建](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-10-14-13-03)、[FFmpeg 對應原始碼](https://github.com/FFmpeg/FFmpeg/tree/7e93014c5b)、[組建腳本](https://github.com/BtbN/FFmpeg-Builds) |
| Send2Trash | 2.1.0，將影片移到資源回收筒 | BSD 3-Clause；[來源](https://github.com/arsenetar/send2trash) |
| pywin32 | 312，Windows 檔案操作 | PSF 類授權；[來源](https://github.com/mhammond/pywin32) |
| PyInstaller | 6.22.3，Windows 打包 | GPLv2 with bootloader exception；[來源](https://pyinstaller.org/) |
| Inno Setup | 6.7.3，建立安裝程式 | [Inno Setup 授權](https://jrsoftware.org/files/is/license.txt) |

發行包使用的 FFmpeg 封存檔為 `ffmpeg-n9.0.2-27-g7e93014c5b-win64-lgpl-shared-9.0.zip`，SHA-256：`75943dd3bb603e49ffe01360ac683e2f17d13c0c4193d68cbd1d11448fa32891`。本專案的 Release 另附對應 FFmpeg 原始碼封存檔。FFmpeg 是獨立執行檔，位於安裝目錄的 `_internal/bin`；可用同授權、相容版本替換其中的執行檔與 DLL。Qt 的 DLL 與外掛位於安裝目錄的 `_internal`，可替換為相容的版本。

此發行包不使用本機測試時的 Gyan `full_build` GPL 組建。自行執行 `build_windows.ps1` 時若不指定 `-FfmpegDir`，腳本會使用系統 PATH 中的 FFmpeg；公開發行前必須確認實際選用組建的授權與附件。
