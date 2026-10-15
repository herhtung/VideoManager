# VideoManager 0.1.0

首個公開 Windows 64 位元安裝版。到下方 Assets 下載 **VideoManager-Setup-0.1.0.exe**，執行後可從開始功能表啟動。程式在本機離線運作，不會上傳影片、索引或標籤。

## 功能

- MP4 縮圖預覽、內建播放器、搜尋、標籤、評分、收藏與統計。
- 可把多個 MP4 組成同一部影片，依序播放並共用整理資料。
- 可索引原位置影片，或把新影片搬入自行指定的影片庫；檔案操作前顯示確認。
- 影格快取在背景產生；開始播放時會暫停背景解碼。

## 安裝與資料

支援 Windows 10／11 64 位元。安裝檔內含 Python、Qt、FFmpeg 與 FFprobe，無須分別安裝。使用者資料位於 `%LOCALAPPDATA%\VideoManager`，影片位於你選定的影片庫。解除安裝不會刪除這些資料。

此版本尚未在所有外接硬碟、編碼與 Windows 設定上驗收。安裝檔未經程式碼簽章；Windows 可能顯示警告。請只從本專案的 Releases 下載並核對 SHA-256：

`VideoManager-Setup-0.1.0.exe`：`BCB5DA82E5327E43B7F1396C91B14CC0E76E0868DC444EA2D4FFE86DAD010A64`

## 第三方原始碼

本版本採用 LGPL shared 版 FFmpeg，另附 `FFmpeg-source-7e93014c5b.zip` 與 `FFmpeg-build-scripts-63ac3265.zip`。其他第三方元件、授權及來源列於 [THIRD_PARTY.md](https://github.com/herhtung/VideoManager/blob/main/packaging/THIRD_PARTY.md)。VideoManager 自身的原始碼公開供檢視，詳見 [LICENSE.md](https://github.com/herhtung/VideoManager/blob/main/LICENSE.md)。
