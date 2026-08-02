# logger.py
import logging
from logging.handlers import RotatingFileHandler
import queue
import tkinter as tk
import os
import sys
import config


def _widget_color(widget, option, fallback):
    try:
        value = widget.cget(option)
        return value or fallback
    except Exception:
        return fallback

class TextHandler(logging.Handler):
    def __init__(self, widget, progress_cb=None):
        super().__init__()
        self.widget = widget
        self.progress_cb = progress_cb
        self.q = queue.Queue()
        self.running = True
        
        # 配置日志文字样式 (增加行高与留白)
        base_fg = _widget_color(self.widget, "fg", config.ThemeConfig.TEXT_MAIN)
        self.widget.tag_configure('info', foreground=base_fg, spacing1=4, spacing3=4)
        self.widget.tag_configure('warning', foreground=config.ThemeConfig.WARNING_COLOR, spacing1=4, spacing3=4)
        self.widget.tag_configure('error', foreground=config.ThemeConfig.ERROR_COLOR, spacing1=4, spacing3=4)
        self.widget.tag_configure('success', foreground=config.ThemeConfig.SUCCESS_COLOR, spacing1=4, spacing3=4)
        self.widget.tag_configure('progress', foreground=config.ThemeConfig.INFO_COLOR, spacing1=4, spacing3=4)
        
        # 元数据键值对分离样式
        self.widget.tag_configure('metadata_key', foreground=config.ThemeConfig.PRIMARY_DARK, font=(config.ThemeConfig.FONT_FAMILY, 10, 'bold'), spacing1=3)
        self.widget.tag_configure('metadata_val', foreground=base_fg, font=(config.ThemeConfig.FONT_FAMILY, 10), spacing1=3)
        
        self.widget.tag_configure('stdout', foreground='#0066CC', font=('Consolas', 9), spacing1=2)
        self.widget.tag_configure('stderr', foreground='#CC0000', font=('Consolas', 9), spacing1=2)
        self._start()

    def emit(self, record):
        msg = self.format(record)
        msg = self.clean_metadata_display(msg)
        
        # 【核心修复】剔除变体选择器 \ufe0f，防止 Tkinter 产生巨型不可见空格导致缩进错乱
        msg = msg.replace('\ufe0f', '')
        
        if record.levelno >= logging.ERROR:
            t = 'error'
        elif record.levelno >= logging.WARNING:
            t = 'warning'
        elif '成功' in msg or '✅' in msg:
            t = 'success'
        elif '进度' in msg or '%' in msg:
            t = 'progress'
        # 使用剔除 \ufe0f 后的基础 Emoji 进行比对
        elif any(icon in msg for icon in ['📋', '💽', '📖', '👤', '🎙', '📚', '📅', '🏷', '🌐', '📊', '🔊', '🎵', '📁', '⏱', '🖼', '📄', '📝']):
            t = 'metadata'
        else:
            t = 'info'
            
        self.q.put((msg, t))

    def clean_metadata_display(self, msg):
        if "📋 写入音频的元数据信息汇总" in msg:
            return msg
        return msg

    def _start(self):
        def proc():
            while not self.q.empty():
                try:
                    msg, t = self.q.get_nowait()
                    self.widget.config(state=tk.NORMAL)
                    
                    # 动态分割元数据的 Key 和 Value
                    if t == 'metadata' and ': ' in msg:
                        key, val = msg.split(': ', 1)
                        self.widget.insert(tk.END, key + ': ', 'metadata_key')
                        self.widget.insert(tk.END, val + '\n', 'metadata_val')
                    else:
                        self.widget.insert(tk.END, msg + '\n', t)
                        
                    self.widget.see(tk.END)
                    self.widget.config(state=tk.DISABLED)
                except queue.Empty:
                    break
            if self.running:
                self.widget.after(100, proc)
        self.widget.after(100, proc)

    def add_console_message(self, msg, stream_type='stdout'):
        if msg.strip():
            if stream_type == 'stderr':
                t = 'error' if 'error' in msg.lower() or 'exception' in msg.lower() else 'stderr'
            else:
                t = 'info'
            self.q.put((f"[控制台 {stream_type}] {msg.rstrip()}", t))

def init_logger(widget, progress_cb=None):
    logger = logging.getLogger("AudioBookTool")
    logger.setLevel(logging.DEBUG)
    
    logger.handlers.clear()
    
    log_file_path = os.path.join(config.SCRIPT_DIR, "audio_processor.log")
    file_handler = RotatingFileHandler(
        log_file_path,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_formatter = logging.Formatter("[%(asctime)s] %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(logging.DEBUG)
    
    fmt = logging.Formatter("%(message)s", datefmt="%H:%M:%S")
    th = TextHandler(widget, progress_cb)
    th.setFormatter(fmt)
    
    logger.addHandler(file_handler)
    logger.addHandler(th)
    
    def handle_unhandled_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.error("❌ 未处理的异常:", exc_info=(exc_type, exc_value, exc_traceback))
        error_msg = f"未处理异常: {exc_type.__name__}: {exc_value}"
        th.add_console_message(f"❌ {error_msg}\n", 'stderr')
    
    sys.excepthook = handle_unhandled_exception
    return logger, th
