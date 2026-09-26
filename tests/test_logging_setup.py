"""日志初始化回归测试：应用结构化日志和控制台捕获都要落盘。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_logging_setup_writes_app_and_console_logs(tmp_path):
    project_dir = Path(__file__).resolve().parents[1]
    code = r'''
import logging
import sys
from pathlib import Path
from service.logging_setup import setup_logging

app_log, console_log = setup_logging(Path(sys.argv[1]))
print("raw console line")
logging.getLogger("test.logger").info("structured app line")
sys.stdout.flush()
sys.stderr.flush()
print(app_log)
print(console_log)
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    app_log = tmp_path / "qq-desktop-service.log"
    console_log = tmp_path / "qq-desktop-service.console.log"
    assert app_log.exists()
    assert console_log.exists()
    assert "structured app line" in app_log.read_text(encoding="utf-8")
    assert "raw console line" in console_log.read_text(encoding="utf-8")