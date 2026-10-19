# 第三方元件與授權

VideoManager 的著作權聲明見專案根目錄的 `LICENSE.md`。以下元件有各自的授權；隨安裝檔附上的授權文字位於安裝目錄的 `_internal/third_party/licenses`，本檔也會一同安裝。

| 元件 | 安裝版本與用途 | 授權及來源 |
|---|---|---|
| Python | 3.12，程式執行環境 | Python Software Foundation License；[來源](https://www.python.org/downloads/source/) |
| PySide6 / Qt / Shiboken6 | 6.12.0，視窗與內建播放器 | LGPLv3 / GPLv3 / 商業授權選項；本包採 LGPLv3，見 [Qt for Python](https://doc.qt.io/qtforpython-6/) 與 [Qt 原始碼](https://code.qt.io/cgit/qt/) |
| FFmpeg / FFprobe | `n9.0.2-27-g7e93014c5b`，壓縮、剪輯與擷取縮圖 | [BtbN LGPLv3 shared 組建](https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-10-14-13-03)；對應原始碼隨 GitHub Release 提供 |
| Kvazaar / OpenH264 | 提供 H.265／H.264 CPU 編碼 | 各自的 BSD 3-Clause 授權，文字隨安裝檔附在 `_internal/third_party/licenses`；[Kvazaar](https://github.com/ultravideo/kvazaar)、[OpenH264](https://github.com/cisco/openh264) |
| libmpv | 額外的 Windows 播放核心 | mpv 官方 `-Dgpl=false` LGPLv3 組建；[官方下載與原始碼](https://github.com/mpv-player/mpv/releases/tag/git-release)，授權文字隨安裝檔附在 `_internal/third_party/licenses/LICENSE.LGPL`。DLL 位於 `_internal/mpv`，可替換為相容的 LGPL 版。 |
| Send2Trash | 2.1.0，將影片移到資源回收筒 | BSD 3-Clause；[來源](https://github.com/arsenetar/send2trash) |
| psutil | 7.2.2，暫停與恢復進行中的 FFmpeg 工作 | BSD 3-Clause；[來源](https://github.com/giampaolo/psutil) |
| pywin32 | 312，Windows 檔案操作 | PSF 類授權；[來源](https://github.com/mhammond/pywin32) |
| PyInstaller | 6.22.3，Windows 打包 | GPLv2 with bootloader exception；[來源](https://pyinstaller.org/) |
| Inno Setup | 6.7.3，建立安裝程式 | [Inno Setup 授權](https://jrsoftware.org/files/is/license.txt) |

發行包使用的 FFmpeg 封存檔為 `ffmpeg-n9.0.2-27-g7e93014c5b-win64-lgpl-shared-9.0.zip`，SHA-256：`75943dd3bb603e49ffe01360ac683e2f17d13c0c4193d68cbd1d11448fa32891`。本專案的 Release 另附對應 FFmpeg 原始碼封存檔。FFmpeg 是獨立執行檔，位於安裝目錄的 `_internal/bin`；可用同授權、相容版本替換其中的執行檔與 DLL。Qt 的 DLL 與外掛位於安裝目錄的 `_internal`，可替換為相容的版本。

公開 0.2.12 發行包使用上表的 LGPL FFmpeg 組建，包含 Kvazaar 與 OpenH264 CPU 編碼器；不打包 Gyan GPL 組建。自行執行 `build_windows.ps1` 時請使用 `-FfmpegDir` 指定 LGPL 組建。VLC 並未放入安裝檔；若使用者電腦已安裝 VLC，播放器可選擇呼叫其 LibVLC 作為額外播放核心。

0.2.12 的 libmpv 取自官方 `libmpv-v0.41.0-dev-gdde067107-38976593432-x86_64-w64-mingw32-lgpl.zip`，SHA-256：`7fec222696953c44da7d1380e59a432e107c9b072a6597c0c0f5cd95544401ba`。只收錄其中的 `libmpv-2.dll` 與 `LICENSE.LGPL`；Qt 播放核心仍可在不使用 libmpv 時獨立運作。
