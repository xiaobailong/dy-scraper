# -*- coding: utf-8 -*-
"""日志模块：支持按天滚动，保留最近7天，同时输出到控制台和文件"""

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path


class DailyRotatingFileHandler(logging.Handler):
    """按天滚动的文件日志处理器，每天一个 .log 文件，自动清理超过7天的旧文件"""

    def __init__(self, log_dir: Path, keep_days: int = 7):
        super().__init__()
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.keep_days = keep_days
        self._current_date = None
        self._file = None
        self._cleanup_old_logs()

    def _get_log_path(self) -> Path:
        date_str = datetime.now().strftime("%Y-%m-%d")
        return self.log_dir / f"{date_str}.log"

    def _open_file(self):
        today = datetime.now().date()
        if self._current_date != today:
            if self._file:
                self._file.close()
            self._current_date = today
            self._file = open(self._get_log_path(), 'a', encoding='utf-8')

    def _cleanup_old_logs(self):
        cutoff = datetime.now() - timedelta(days=self.keep_days)
        for f in self.log_dir.glob("*.log"):
            try:
                file_date = datetime.strptime(f.stem, "%Y-%m-%d")
                if file_date < cutoff:
                    f.unlink()
            except (ValueError, OSError):
                pass

    def emit(self, record):
        self._open_file()
        msg = self.format(record)
        if self._file:
            self._file.write(msg + '\n')
            self._file.flush()

    def close(self):
        if self._file:
            self._file.close()
            self._file = None
        super().close()


def setup_logger(name: str = "dy-scraper", log_dir: Path = None) -> logging.Logger:
    """配置并返回 logger，同时输出到控制台和按天滚动的日志文件"""
    if log_dir is None:
        log_dir = Path(__file__).parent.parent / "logs"

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    if logger.handlers:
        return logger

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(console_handler)

    file_handler = DailyRotatingFileHandler(log_dir, keep_days=7)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(file_handler)

    return logger


_logger = None


def log(msg: str = "", level: str = "info"):
    """便捷函数：同时输出到控制台和日志文件，用法与 print() 一致"""
    global _logger
    if _logger is None:
        _logger = setup_logger()
    getattr(_logger, level)(msg)