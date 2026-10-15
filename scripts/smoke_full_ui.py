"""Exercise a disposable library through scan, cache, and player creation."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import QApplication, QListView

from dataclasses import replace

from videomanager.config import AppPaths, load_settings, save_settings
from videomanager.db import repository as repo
from videomanager.services.library_service import open_library
from videomanager.ui.main_window import MainWindow
from videomanager.ui.metadata_dialog import MetadataDialog
from videomanager.ui.filter_dialog import FilterDialog
from videomanager.ui.tag_picker import TagCreateDialog


def main() -> int:
    app = QApplication([])
    with tempfile.TemporaryDirectory(prefix="vm-ui-", dir=Path(__file__).resolve().parent) as scratch:
        base = Path(scratch)
        root = base / "library"
        root.mkdir()
        (root / "chosen").mkdir()
        video = root / "chosen" / "影片 測試.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-f", "lavfi", "-i", "color=c=red:s=320x180:r=24",
             "-t", "12", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(video)],
            check=True, timeout=30,
        )
        paths = AppPaths(base / "data")
        open_library(paths, root, create=True)
        save_settings(paths.settings, replace(load_settings(paths.settings),
                                              scan_folders=("chosen",), preview_policy="run"))
        window = MainWindow(paths)
        window.show()
        state = {"scanned": False, "started": False, "triage": False, "player": False, "player_modes": False}

        def check() -> None:
            if window.worker is not None:
                return
            if window.model.rowCount() != 1:
                return
            state["scanned"] = True
            if not state["started"]:
                state["started"] = True
                grid_width = window.browser.visualRect(window.model.index(0)).width()
                window.toggle_list()
                assert window.browser.viewMode() == QListView.ListMode
                assert window.browser.visualRect(window.model.index(0)).width() > grid_width
                window.toggle_list()
                assert window.browser.viewMode() == QListView.IconMode
                row = window.model.rows[0]
                window.browser.setCurrentIndex(window.model.index(0))
                window.details.notes.setPlainText("已開始整理")
                window.details.rating.buttons[3].click()
                assert window.details.rating.value() == 4
                def create_tag(dialog):
                    dialog.name.setText("快速新增")
                    dialog._create()
                    return dialog.result()

                with patch.object(TagCreateDialog, "exec", create_tag):
                    window.details.quick_add_tag()
                assert window.details.save_current()
                assert repo.video_by_id(window.connection, row["id"])["status"] == "in_progress"
                assert repo.video_by_id(window.connection, row["id"])["rating"] == 4
                assert any(tag["name"] == "快速新增" for tag in repo.list_tags(window.connection))
                tag_id = next(tag["id"] for tag in repo.list_tags(window.connection) if tag["name"] == "快速新增")
                filters = FilterDialog(window.connection, {"tag_all": [tag_id]}, window)
                filters.show()
                assert filters.tag_all.mapToGlobal(QPoint(0, 0)).y() < filters.duration_min.mapToGlobal(QPoint(0, 0)).y()
                assert filters.filters()["tag_all"] == [tag_id]
                filters.tag_any._buttons[tag_id][0].click()
                assert filters.filters()["tag_any"] == [tag_id]
                filters.close()
                editor = MetadataDialog(window.connection, window)
                editor.show()
                editor.new_category_name.setText("測試分類")
                editor.add_category()
                moved_category_id = editor.categories.selected_id()
                assert moved_category_id is not None
                editor.new_tag_name.setText("分類內標籤")
                editor.add_tag()
                editor.tags.search.setText("分類內")
                assert editor._selected_tag_id in editor.tags._buttons
                editor._tag_selected(tag_id)
                editor.categories.select(moved_category_id)
                editor.move_category(-1)
                assert editor.categories.selected_id() == moved_category_id
                assert editor.categories._buttons[moved_category_id].isChecked()
                assert editor._selected_tag_id == tag_id
                editor.close()
                window.details.finish_current()
                assert repo.video_by_id(window.connection, row["id"])["status"] == "done"
                state["triage"] = True
                window.open_player(row["id"])
                state["player"] = window.player is not None
                QTimer.singleShot(700, check_player_modes)

        def check_player_modes() -> None:
            player = window.player
            assert player is not None
            assert player.isMaximized()
            assert not player.details_overlay.isVisible()
            player.player.pause()
            player.player.setPosition(0)
            player.video.setFocus()
            QTest.keyClick(player.video, Qt.Key_Up)
            assert player.volume.value() == 75
            QTest.keyClick(player.video, Qt.Key_Down)
            assert player.volume.value() == 70
            QTest.keyClick(player.video, Qt.Key_Right)
            assert player.player.position() >= 9000
            QTest.keyClick(player.video, Qt.Key_Left)
            assert player.player.position() <= 1000
            original_video_width = player.video.width()
            player.toggle_details()
            assert player.details_overlay.isVisible()
            assert player.video.width() == original_video_width
            player.toggle_details()
            assert not player.details_overlay.isVisible()
            player.seek.setRange(0, 1000)
            clicked = []
            player.seek.clicked_position.connect(clicked.append)
            QTest.mouseClick(player.seek, Qt.LeftButton, pos=QPoint(player.seek.width() * 3 // 4, player.seek.height() // 2))
            assert clicked and 700 <= clicked[-1] <= 800
            player.toggle_fullscreen()
            assert player.isFullScreen()
            assert player.fullscreen_button.text() == "退出全螢幕"
            assert player.fullscreen_button.isVisible()
            player.exit_fullscreen()
            app.processEvents()
            assert player.isMaximized()
            player.toggle_maximized()
            assert not player.isMaximized()
            state["player_modes"] = True
            QTimer.singleShot(100, finish)

        def finish() -> None:
            if window.player:
                window.player.close()
                app.processEvents()
            app.quit()

        timer = QTimer()
        timer.setInterval(100)
        timer.timeout.connect(check)
        timer.start()
        QTimer.singleShot(20000, finish)
        app.exec()
        timer.stop()
        frame_files = list(paths.cache.rglob("*.webp"))
        window.close()
        app.processEvents()
        if not all(state.values()) or len(frame_files) != 10:
            raise RuntimeError(f"UI integration failed: {state}, frames={len(frame_files)}")
        print("UI integration passed: scan, triage state, 10 frames, player window modes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
