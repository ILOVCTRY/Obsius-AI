# proxy_pool/main.py

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, TclError
from tkinter import filedialog
import ttkbootstrap as bs
import queue
import threading
from datetime import datetime
import re
import json
import os
import base64
from pathlib import Path
from urllib.parse import urlparse

# Monochrome palette used by the whole desktop UI. Keeping it in one place
# prevents ttkbootstrap's semantic button colors from leaking into the theme.
MONOCHROME = {
    'black': '#000000',
    'near_black': '#111111',
    'white': '#ffffff',
    'light': '#f2f2f2',
    'mid': '#808080',
    'muted': '#666666',
}
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


def configure_monochrome_theme(window):
    """Apply a black/white ttk theme to the main window and its dialogs."""
    style = ttk.Style(window)
    try:
        # clam exposes the standard ttk color options consistently on Windows.
        style.theme_use('clam')
    except TclError:
        pass

    black = MONOCHROME['black']
    near_black = MONOCHROME['near_black']
    white = MONOCHROME['white']
    mid = MONOCHROME['mid']

    style.configure('.', background=black, foreground=white)
    style.configure('TFrame', background=black)
    style.configure('TLabel', background=black, foreground=white)
    style.configure('TLabelframe', background=black, foreground=white,
                    bordercolor=white, lightcolor=black, darkcolor=black)
    style.configure('TLabelframe.Label', background=black, foreground=white)
    style.configure('TNotebook', background=black, bordercolor=white,
                    lightcolor=black, darkcolor=black)
    style.configure('TNotebook.Tab', background=black, foreground=white,
                    bordercolor=white, lightcolor=black, darkcolor=black,
                    padding=(10, 5))
    style.map('TNotebook.Tab', background=[('selected', white), ('active', near_black)],
              foreground=[('selected', black), ('active', white)])

    style.configure('TButton', background=black, foreground=white,
                    bordercolor=white, lightcolor=black, darkcolor=black,
                    focusthickness=1, relief='solid', padding=(8, 5))
    button_styles = (
        'success.TButton', 'primary.TButton', 'warning.TButton',
        'danger.TButton', 'info.TButton', 'secondary.TButton',
        'success.outline.TButton', 'info.outline.TButton',
        'success.Large.TButton', 'info.Large.TButton', 'Large.TButton',
    )
    for button_style in button_styles:
        style.configure(button_style, background=black, foreground=white,
                        bordercolor=white, lightcolor=black, darkcolor=black,
                        focusthickness=1, relief='solid')
        style.map(button_style,
                  background=[('active', white), ('pressed', white), ('disabled', near_black)],
                  foreground=[('active', black), ('pressed', black), ('disabled', mid)],
                  bordercolor=[('disabled', mid)])

    style.configure('TEntry', fieldbackground=black, foreground=white,
                    insertcolor=white, bordercolor=white,
                    lightcolor=black, darkcolor=black)
    style.configure('TSpinbox', fieldbackground=black, foreground=white,
                    arrowcolor=white, bordercolor=white,
                    lightcolor=black, darkcolor=black)
    style.configure('TCombobox', fieldbackground=black, background=black,
                    foreground=white, arrowcolor=white,
                    bordercolor=white, lightcolor=black, darkcolor=black)
    style.map('TCombobox', fieldbackground=[('readonly', black), ('disabled', near_black)],
              foreground=[('readonly', white), ('disabled', mid)])
    style.configure('TCheckbutton', background=black, foreground=white,
                    indicatorcolor=black, indicatormargin=4,
                    bordercolor=white, lightcolor=black, darkcolor=black)
    style.map('TCheckbutton', indicatorcolor=[('selected', white), ('active', near_black)],
              foreground=[('disabled', mid)])

    style.configure('Treeview', background=black, fieldbackground=black,
                    foreground=white, bordercolor=white, lightcolor=black,
                    darkcolor=black, rowheight=28)
    style.map('Treeview', background=[('selected', white)],
              foreground=[('selected', black)])
    style.configure('Treeview.Heading', background=black, foreground=white,
                    bordercolor=white, lightcolor=black, darkcolor=black,
                    relief='solid')
    style.map('Treeview.Heading', background=[('active', white)],
              foreground=[('active', black)])
    style.configure('TPanedwindow', background=black, sashthickness=2)
    style.configure('TScrollbar', background=black, troughcolor=near_black,
                    arrowcolor=white, bordercolor=white,
                    lightcolor=black, darkcolor=black)
    style.configure('success.Striped.Horizontal.TProgressbar',
                    background=white, troughcolor=near_black,
                    bordercolor=white, lightcolor=white, darkcolor=white)

    window.configure(background=black)

# 导入核心模块
from modules.fetcher import ProxyFetcher
from modules.checker import ProxyChecker
from modules.rotator import ProxyRotator
from modules.server import ProxyServer
from modules.asset_searcher import AssetSearcher

class SettingsWindow(tk.Toplevel):
    """设置窗口的UI和逻辑, 包含通用设置和自动爬取功能。"""
    def __init__(self, parent_app, current_settings, callbacks):
        super().__init__(parent_app.root)
        self.configure(background=MONOCHROME['black'])
        self.transient(parent_app.root)
        self.grab_set()
        self.title("设置")
        self.parent_app = parent_app
        self.settings = current_settings
        self.save_callback = callbacks['save']
        self.search_callback = callbacks['search']

        self.resizable(False, False)

        # --- 创建主框架和选项卡 ---
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(pady=10, padx=10, fill="both", expand=True)

        self.general_frame = ttk.Frame(self.notebook, padding=15)
        self.auto_fetch_frame = ttk.Frame(self.notebook, padding=15)

        self.notebook.add(self.general_frame, text='通用设置')
        self.notebook.add(self.auto_fetch_frame, text='自动爬取')
        
        # --- 初始化所有设置变量 ---
        self._init_vars()

        # --- 创建两个选项卡的内容 ---
        self._create_general_tab()
        self._create_auto_fetch_tab()
        
        # --- 创建底部按钮 ---
        self.button_frame = ttk.Frame(self)
        self.button_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(5, 15))
        self._create_buttons()

        # --- 绑定事件 ---
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        
        self.center_window()
        self._on_tab_changed() # 初始化按钮状态

    def _init_vars(self):
        """初始化所有Tkinter变量"""
        # 通用设置
        general_cfg = self.settings.get('general', {})
        self.validation_threads_var = tk.IntVar(value=general_cfg.get('validation_threads', 100))
        self.failure_threshold_var = tk.IntVar(value=general_cfg.get('failure_threshold', 3))
        self.auto_retest_enabled_var = tk.BooleanVar(value=general_cfg.get('auto_retest_enabled', False))
        self.auto_retest_interval_var = tk.IntVar(value=general_cfg.get('auto_retest_interval', 10))
        self.url_health_enabled_var = tk.BooleanVar(value=general_cfg.get('url_health_enabled', False))
        self.url_health_interval_var = tk.IntVar(value=general_cfg.get('url_health_interval_seconds', 60))
        self.url_health_urls_text = general_cfg.get(
            'url_health_urls', general_cfg.get('url_health_url', 'https://www.baidu.com')
        )
        self.request_rotation_enabled_var = tk.BooleanVar(
            value=general_cfg.get('request_rotation_enabled', False)
        )
        self.request_rotation_count_var = tk.IntVar(
            value=general_cfg.get('request_rotation_count', 10)
        )
        self.target_failover_enabled_var = tk.BooleanVar(
            value=general_cfg.get('target_failover_enabled', False)
        )
        self.target_failover_threshold_var = tk.IntVar(
            value=general_cfg.get('target_failover_threshold', 3)
        )

        # 自动爬取设置
        fetch_cfg = self.settings.get('auto_fetch', {})
        # Fofa
        fofa_cfg = fetch_cfg.get('fofa', {})
        self.fofa_enabled_var = tk.BooleanVar(value=fofa_cfg.get('enabled', True))
        self.fofa_key_var = tk.StringVar(value=fofa_cfg.get('key', ''))
        self.fofa_query_var = tk.StringVar(value=fofa_cfg.get('query', 'protocol=="socks5" && country=="CN" && banner="Method:No"'))
        self.fofa_size_var = tk.IntVar(value=fofa_cfg.get('size', 500))
        # Hunter
        hunter_cfg = fetch_cfg.get('hunter', {})
        self.hunter_enabled_var = tk.BooleanVar(value=hunter_cfg.get('enabled', False))
        self.hunter_key_var = tk.StringVar(value=hunter_cfg.get('key', ''))
        self.hunter_query_var = tk.StringVar(value=hunter_cfg.get('query', ''))
        self.hunter_size_var = tk.IntVar(value=hunter_cfg.get('size', 200))

    def _create_general_tab(self):
        """创建通用设置选项卡的内容"""
        validation_frame = ttk.Labelframe(self.general_frame, text="验证设置", padding=10)
        validation_frame.pack(fill=tk.X, expand=True, pady=(0, 10))
        ttk.Label(validation_frame, text="质量验证线程数:").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Spinbox(validation_frame, from_=10, to=500, increment=10, textvariable=self.validation_threads_var, width=15).pack(side=tk.LEFT)

        failure_frame = ttk.Labelframe(self.general_frame, text="失败代理清理设置", padding=10)
        failure_frame.pack(fill=tk.X, expand=True, pady=(0, 10))
        ttk.Label(failure_frame, text="连续失败阈值:").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Spinbox(failure_frame, from_=1, to=10, textvariable=self.failure_threshold_var, width=15).pack(side=tk.LEFT)
        
        retest_frame = ttk.Labelframe(self.general_frame, text="自动重测设置", padding=10)
        retest_frame.pack(fill=tk.X, expand=True, pady=(0, 10))
        ttk.Checkbutton(retest_frame, text="启用代理池自动重测", variable=self.auto_retest_enabled_var).pack(anchor='w')
        
        retest_interval_frame = ttk.Frame(retest_frame)
        retest_interval_frame.pack(fill=tk.X, expand=True, pady=(5,0))
        ttk.Label(retest_interval_frame, text="重测间隔 (分钟):").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Spinbox(retest_interval_frame, from_=1, to=120, textvariable=self.auto_retest_interval_var, width=15).pack(side=tk.LEFT)

        health_frame = ttk.Labelframe(self.general_frame, text="代理URL健康检查", padding=10)
        health_frame.pack(fill=tk.X, expand=True, pady=(0, 10))
        ttk.Checkbutton(
            health_frame, text="启用URL健康检查",
            variable=self.url_health_enabled_var
        ).pack(anchor='w')
        health_interval_frame = ttk.Frame(health_frame)
        health_interval_frame.pack(fill=tk.X, expand=True, pady=(5, 0))
        ttk.Label(health_interval_frame, text="检查间隔 (秒):").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Spinbox(
            health_interval_frame, from_=10, to=86400,
            textvariable=self.url_health_interval_var, width=10
        ).pack(side=tk.LEFT)
        ttk.Label(health_frame, text="测试地址（每行一个；域名/IP可不写 http://）:").pack(
            anchor='w', pady=(8, 2)
        )
        self.url_health_urls_widget = tk.Text(
            health_frame, height=5, width=65, wrap=tk.WORD,
            bg=MONOCHROME['black'], fg=MONOCHROME['white'],
            insertbackground=MONOCHROME['white'],
            selectbackground=MONOCHROME['white'],
            selectforeground=MONOCHROME['black'],
            highlightbackground=MONOCHROME['white'],
            highlightcolor=MONOCHROME['white'],
        )
        self.url_health_urls_widget.insert('1.0', self.url_health_urls_text)
        self.url_health_urls_widget.pack(fill=tk.X, expand=True)
        ttk.Label(
            health_frame, text="所有地址按周期测试；地址失败会记录健康状态。目标失败自动切换请在下方单独启用。"
        ).pack(anchor='w', pady=(5, 0))

        request_rotation_frame = ttk.Labelframe(
            self.general_frame, text="按请求数轮换", padding=10
        )
        request_rotation_frame.pack(fill=tk.X, expand=True, pady=(0, 10))
        ttk.Checkbutton(
            request_rotation_frame, text="启用按成功转发请求数自动切换代理",
            variable=self.request_rotation_enabled_var
        ).pack(side=tk.LEFT)
        ttk.Label(request_rotation_frame, text="每").pack(side=tk.LEFT, padx=(12, 4))
        ttk.Spinbox(
            request_rotation_frame, from_=1, to=100000,
            textvariable=self.request_rotation_count_var, width=8
        ).pack(side=tk.LEFT)
        ttk.Label(request_rotation_frame, text="个请求切换一次").pack(side=tk.LEFT, padx=(4, 0))

        target_failover_frame = ttk.Labelframe(
            self.general_frame, text="目标连接失败切换", padding=10
        )
        target_failover_frame.pack(fill=tk.X, expand=True, pady=(0, 10))
        ttk.Checkbutton(
            target_failover_frame, text="启用同一域名/IP连续连接失败自动切换",
            variable=self.target_failover_enabled_var
        ).pack(side=tk.LEFT)
        ttk.Label(target_failover_frame, text="连续失败").pack(side=tk.LEFT, padx=(12, 4))
        ttk.Spinbox(
            target_failover_frame, from_=1, to=100,
            textvariable=self.target_failover_threshold_var, width=8
        ).pack(side=tk.LEFT)
        ttk.Label(target_failover_frame, text="次后切换").pack(side=tk.LEFT, padx=(4, 0))

    def _create_auto_fetch_tab(self):
        """创建自动爬取选项卡的内容"""
        # --- FOFA ---
        fofa_frame = ttk.Labelframe(self.auto_fetch_frame, text="Fofa", padding=10)
        fofa_frame.pack(fill=tk.X, pady=5)
        fofa_frame.grid_columnconfigure(2, weight=1)
        
        ttk.Checkbutton(fofa_frame, text="启用", variable=self.fofa_enabled_var).grid(row=0, column=0, padx=5)
        ttk.Label(fofa_frame, text="查询数量:").grid(row=0, column=1, padx=5, sticky='e')
        ttk.Spinbox(fofa_frame, from_=1, to=10000, textvariable=self.fofa_size_var, width=8).grid(row=0, column=2, sticky='w')
        ttk.Label(fofa_frame, text="FofaKey:").grid(row=0, column=3, padx=5)
        ttk.Entry(fofa_frame, textvariable=self.fofa_key_var, width=35).grid(row=0, column=4, padx=5)

        ttk.Label(fofa_frame, text="Fofa语法:").grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky='w')
        ttk.Entry(fofa_frame, textvariable=self.fofa_query_var).grid(row=1, column=2, columnspan=3, padx=5, pady=5, sticky='ew')
        
        # --- Hunter ---
        hunter_frame = ttk.Labelframe(self.auto_fetch_frame, text="Hunter", padding=10)
        hunter_frame.pack(fill=tk.X, pady=5)
        hunter_frame.grid_columnconfigure(2, weight=1)

        ttk.Checkbutton(hunter_frame, text="启用", variable=self.hunter_enabled_var).grid(row=0, column=0, padx=5)
        ttk.Label(hunter_frame, text="查询数量:").grid(row=0, column=1, padx=5, sticky='e')
        ttk.Spinbox(hunter_frame, from_=1, to=1000, textvariable=self.hunter_size_var, width=8).grid(row=0, column=2, sticky='w')
        ttk.Label(hunter_frame, text="HunterKey:").grid(row=0, column=3, padx=5)
        ttk.Entry(hunter_frame, textvariable=self.hunter_key_var, width=35).grid(row=0, column=4, padx=5)

        ttk.Label(hunter_frame, text="Hunter语法:").grid(row=1, column=0, columnspan=2, padx=5, pady=5, sticky='w')
        ttk.Entry(hunter_frame, textvariable=self.hunter_query_var).grid(row=1, column=2, columnspan=3, padx=5, pady=5, sticky='ew')

    def _create_buttons(self):
        """创建底部按钮并居中"""
        for widget in self.button_frame.winfo_children():
            widget.destroy()
        
        style = ttk.Style()
        style.configure('Large.TButton', padding=(10, 8))
        
        current_tab_index = self.notebook.index(self.notebook.select())
        
        # Configure the grid to have expanding empty columns on both sides
        self.button_frame.grid_columnconfigure(0, weight=1)
        self.button_frame.grid_columnconfigure(4, weight=1)

        if current_tab_index == 0: # 通用设置
            self.button_frame.grid_columnconfigure(1, weight=0)
            self.button_frame.grid_columnconfigure(2, weight=0)
            self.button_frame.grid_columnconfigure(3, weight=0)
            
            ttk.Button(self.button_frame, text="保存", command=self.save_and_close, style='success.Large.TButton').grid(row=0, column=1, padx=5)
            ttk.Button(self.button_frame, text="取消", command=self.destroy, style='Large.TButton').grid(row=0, column=2, padx=5)
            
        else: # 自动爬取
            self.button_frame.grid_columnconfigure(1, weight=0)
            self.button_frame.grid_columnconfigure(2, weight=0)
            self.button_frame.grid_columnconfigure(3, weight=0)

            ttk.Button(self.button_frame, text="开始搜索", command=self.save_and_search, style='success.Large.TButton').grid(row=0, column=1, padx=5)
            ttk.Button(self.button_frame, text="保存设置", command=self.save_and_close, style='info.Large.TButton').grid(row=0, column=2, padx=5)
            ttk.Button(self.button_frame, text="取消", command=self.destroy, style='Large.TButton').grid(row=0, column=3, padx=5)


    def _on_tab_changed(self, event=None):
        """当选项卡切换时，重新创建按钮"""
        self._create_buttons()

    def _collect_settings(self):
        """从所有变量中收集设置数据"""
        return {
            'general': {
                'validation_threads': self.validation_threads_var.get(),
                'failure_threshold': self.failure_threshold_var.get(),
                'auto_retest_enabled': self.auto_retest_enabled_var.get(),
                'auto_retest_interval': self.auto_retest_interval_var.get(),
                'url_health_enabled': self.url_health_enabled_var.get(),
                'url_health_interval_seconds': max(10, self.url_health_interval_var.get()),
                'url_health_urls': self.url_health_urls_widget.get('1.0', tk.END).strip(),
                'request_rotation_enabled': self.request_rotation_enabled_var.get(),
                'request_rotation_count': max(1, self.request_rotation_count_var.get()),
                'target_failover_enabled': self.target_failover_enabled_var.get(),
                'target_failover_threshold': max(1, self.target_failover_threshold_var.get()),
            },
            'auto_fetch': {
                'fofa': {
                    'enabled': self.fofa_enabled_var.get(),
                    'key': self.fofa_key_var.get(),
                    'query': self.fofa_query_var.get(),
                    'size': self.fofa_size_var.get(),
                },
                'hunter': {
                    'enabled': self.hunter_enabled_var.get(),
                    'key': self.hunter_key_var.get(),
                    'query': self.hunter_query_var.get(),
                    'size': self.hunter_size_var.get(),
                },
            }
        }

    def save_and_close(self):
        """保存设置并关闭窗口"""
        all_settings = self._collect_settings()
        self.save_callback(all_settings)
        self.destroy()

    def save_and_search(self):
        """保存设置，然后触发搜索，并关闭窗口"""
        all_settings = self._collect_settings()
        self.save_callback(all_settings)
        self.search_callback()
        self.destroy()

    def center_window(self):
        self.update_idletasks()
        parent = self.parent_app.root
        parent_x = parent.winfo_x()
        parent_y = parent.winfo_y()
        parent_w = parent.winfo_width()
        parent_h = parent.winfo_height()
        w = self.winfo_width()
        h = self.winfo_height()
        x = parent_x + (parent_w // 2) - (w // 2)
        y = parent_y + (parent_h // 2) - (h // 2)
        self.geometry(f'{w}x{h}+{x}+{y}')


class ProxyPoolApp:
    def __init__(self, root):
        self.root = root
        self.root.title("高可用代理池 1.6 版本 by firefly")
        self.root.geometry("1200x850")
        self.root.state('zoomed') 
        self.root.minsize(1100, 700)
        
        self.settings = {
            'general': {
                'validation_threads': 100,
                'failure_threshold': 3,
                'auto_retest_enabled': False,
                'auto_retest_interval': 10,
                'url_health_enabled': False,
                'url_health_interval_seconds': 60,
                'url_health_urls': 'https://www.baidu.com',
                'request_rotation_enabled': False,
                'request_rotation_count': 10,
                'target_failover_enabled': False,
                'target_failover_threshold': 3,
            },
            'auto_fetch': {
                'fofa': {'enabled': True, 'key': '', 'query': 'protocol=="socks5" && country=="CN" && banner="Method:No"', 'size': 500},
                'hunter': {'enabled': False, 'key': '', 'query': 'app.name="SOCKS5"', 'size': 100},
            }
        }

        self.result_queue = queue.Queue()
        self.log_queue = queue.Queue()
        self.traffic_queue = queue.Queue()
        self.is_running_task = False
        self.cancel_event = threading.Event()

        self.fetcher = ProxyFetcher()
        self.asset_searcher = AssetSearcher(self.log_queue)
        self.checker = ProxyChecker()
        self.rotator = ProxyRotator()
        self.displayed_proxies = set()
        self.proxy_to_tree_item_map = {}

        self.proxy_server = ProxyServer(
            http_host='127.0.0.1', http_port=1801,
            socks5_host='127.0.0.1', socks5_port=1800,
            rotator=self.rotator, log_queue=self.log_queue,
            traffic_queue=self.traffic_queue
        )
        self.is_server_running = False

        self.is_auto_rotating = False
        self.auto_rotate_job_id = None
        self.auto_retest_job_id = None
        self.url_health_job_id = None
        self.url_health_running = False
        
        self.use_quality_filter_var = tk.BooleanVar(value=False)
        self.quality_latency_var = tk.StringVar(value="2000")

        # --- MODIFIED: Initialization order changed ---
        # 1. Create widgets first, so self.log_text exists
        self._create_widgets()
        
        # 2. Now it's safe to load settings, which might call self.log()
        self.load_settings_from_file()
        self._apply_proxy_rotation_settings()

        # 3. Set up remaining parts of the application
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        threading.Thread(target=self.checker.initialize_public_ip, args=(self.log_queue,), daemon=True).start()
        threading.Thread(target=self._run_builtin_check, daemon=True).start()
        self.process_log_queue()

    def _create_widgets(self):
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame.rowconfigure(2, weight=1)
        main_frame.columnconfigure(0, weight=1)

        top_frame = ttk.Frame(main_frame)
        top_frame.grid(row=0, column=0, sticky='ew', pady=(0, 10))

        actions_frame = ttk.Labelframe(top_frame, text="代理操作")
        actions_frame.pack(side=tk.LEFT, padx=(0, 5), fill=tk.Y)
        
        self.fetch_button = ttk.Button(actions_frame, text="获取代理", command=self.start_fetch_validate_thread, style='success.TButton', width=12)
        self.fetch_button.pack(side=tk.LEFT, padx=(0, 10), pady=5)

        self.import_button = ttk.Button(actions_frame, text="导入代理", command=self.import_and_validate_proxies, style='primary.TButton', width=12)
        self.import_button.pack(side=tk.LEFT, padx=(0, 10), pady=5)
        
        self.cancel_button = ttk.Button(actions_frame, text="取消任务", command=self.cancel_current_task, style='warning.TButton', width=12, state=tk.DISABLED)
        self.cancel_button.pack(side=tk.LEFT, padx=(0, 10), pady=5)
        
        self.clear_button = ttk.Button(actions_frame, text="清空列表", command=self.clear_all_proxies, style='danger.TButton', width=12)
        self.clear_button.pack(side=tk.LEFT, padx=(0, 10), pady=5)

        self.test_all_button = ttk.Button(actions_frame, text="全部重测", command=self.start_revalidate_thread, state=tk.DISABLED, style='info.outline.TButton', width=12)
        self.test_all_button.pack(side=tk.LEFT, padx=(0, 10), pady=5)
        
        self.export_button = ttk.Button(actions_frame, text="导出代理", command=self.export_proxies, state=tk.DISABLED, style='primary.TButton', width=12)
        self.export_button.pack(side=tk.LEFT, padx=(0, 10), pady=5)

        self.settings_button = ttk.Button(actions_frame, text="设置", command=self.open_settings_window, style='info.TButton', width=8)
        self.settings_button.pack(side=tk.LEFT, padx=(0, 5), pady=5)

        region_panel = ttk.Labelframe(top_frame, text="筛选与轮换")
        region_panel.pack(side=tk.LEFT, padx=5, fill=tk.Y)
        
        self.region_combobox = ttk.Combobox(region_panel, state="readonly", width=16)
        self.region_combobox.pack(side=tk.LEFT, padx=5, pady=5)
        self.region_combobox.bind('<<ComboboxSelected>>', self._refresh_treeview)
        self.region_combobox.set("全部国家")

        quality_filter_frame = ttk.Frame(region_panel)
        quality_filter_frame.pack(side=tk.LEFT, padx=5, pady=5)
        
        self.quality_checkbutton = ttk.Checkbutton(quality_filter_frame, text="优质", variable=self.use_quality_filter_var, command=self._refresh_treeview)
        self.quality_checkbutton.pack(side=tk.LEFT)

        ttk.Label(quality_filter_frame, text="ms <").pack(side=tk.LEFT, padx=(5, 2))
        self.quality_latency_entry = ttk.Entry(quality_filter_frame, textvariable=self.quality_latency_var, width=6)
        self.quality_latency_entry.pack(side=tk.LEFT)
        self.quality_latency_entry.bind('<KeyRelease>', self._refresh_treeview)

        self.rotate_button = ttk.Button(region_panel, text="轮换IP", command=self.rotate_proxy, state=tk.DISABLED, width=8)
        self.rotate_button.pack(side=tk.LEFT, padx=5, pady=5)
        
        self.auto_rotate_button = ttk.Button(region_panel, text="自动", command=self.toggle_auto_rotate, state=tk.DISABLED, style='info.TButton', width=6)
        self.auto_rotate_button.pack(side=tk.LEFT, padx=5, pady=5)

        self.interval_spinbox = ttk.Spinbox(region_panel, from_=1, to=300, width=4)
        self.interval_spinbox.set("10")
        self.interval_spinbox.pack(side=tk.LEFT, padx=(0, 5), pady=5)
        ttk.Label(region_panel, text="秒").pack(side=tk.LEFT, padx=(0,5), pady=5)

        service_status_panel = ttk.Labelframe(top_frame, text="代理服务与状态 (SOCKS5:1800 / HTTP:1801)")
        service_status_panel.pack(side=tk.LEFT, padx=5, fill=tk.Y)

        self.server_button = ttk.Button(service_status_panel, text="启动服务", command=self.toggle_server, state=tk.DISABLED, style='info.TButton', width=12)
        self.server_button.pack(side=tk.LEFT, padx=5, pady=5)
        
        self.current_proxy_var = tk.StringVar(value="当前使用: N/A")
        proxy_entry = ttk.Entry(service_status_panel, textvariable=self.current_proxy_var, state='readonly', width=30)
        proxy_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5,5), pady=5)

        self.progress_bar = ttk.Progressbar(main_frame, mode='determinate', style='success.Striped.Horizontal.TProgressbar')
        self.progress_bar.grid(row=1, column=0, sticky='ew', pady=5)
        paned_window = ttk.PanedWindow(main_frame, orient=tk.VERTICAL)
        paned_window.grid(row=2, column=0, sticky='nsew')
        list_frame = ttk.Labelframe(paned_window, text="可用代理列表 (右键操作)", padding=10)
        paned_window.add(list_frame, weight=3)
        
        columns = ('score', 'anonymity', 'protocol', 'proxy', 'delay', 'speed', 'region')
        self.tree = ttk.Treeview(list_frame, columns=columns, show='headings', height=20)
        
        self.tree.heading('score', text='分数', command=lambda: self.sort_treeview_column('score', True))
        self.tree.heading('anonymity', text='匿名度', command=lambda: self.sort_treeview_column('anonymity', False))
        self.tree.heading('protocol', text='协议', command=lambda: self.sort_treeview_column('protocol', False))
        self.tree.heading('proxy', text='代理地址')
        self.tree.heading('delay', text='延迟(ms)', command=lambda: self.sort_treeview_column('delay', False))
        self.tree.heading('speed', text='速度(Mbps)', command=lambda: self.sort_treeview_column('speed', True))
        self.tree.heading('region', text='国家/地区')
        
        self.tree.column('score', width=70, anchor='center'); self.tree.column('anonymity', width=80, anchor='center')
        self.tree.column('protocol', width=60, anchor='center'); self.tree.column('proxy', width=180)
        self.tree.column('delay', width=80, anchor='center'); self.tree.column('speed', width=90, anchor='center')
        self.tree.column('region', width=120, anchor='center')

        self.tree.tag_configure('unavailable', foreground=MONOCHROME['muted'])
        
        self.tree.bind("<Double-1>", self.copy_to_clipboard)
        self.tree.bind("<Button-3>", self._show_context_menu)

        tree_scroll_y = ttk.Scrollbar(list_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll_y.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        
        log_frame = ttk.Labelframe(paned_window, text="实时日志", padding=10)
        paned_window.add(log_frame, weight=1)
        self.log_frame = log_frame
        log_frame.columnconfigure(0, weight=1)
        log_frame.columnconfigure(1, weight=1)
        log_frame.rowconfigure(1, weight=1)

        ttk.Label(log_frame, text="系统日志").grid(
            row=0, column=0, sticky='w', pady=(0, 5)
        )
        ttk.Label(log_frame, text="代理流量").grid(
            row=0, column=1, sticky='w', padx=(10, 0), pady=(0, 5)
        )
        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, state='disabled',
            bg=MONOCHROME['black'], fg=MONOCHROME['white'],
            insertbackground=MONOCHROME['white'],
            selectbackground=MONOCHROME['white'],
            selectforeground=MONOCHROME['black'],
            highlightbackground=MONOCHROME['white'],
            highlightcolor=MONOCHROME['white'],
        )
        self.log_text.grid(row=1, column=0, sticky='nsew')
        self.traffic_text = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, state='disabled',
            bg=MONOCHROME['black'], fg=MONOCHROME['white'],
            insertbackground=MONOCHROME['white'],
            selectbackground=MONOCHROME['white'],
            selectforeground=MONOCHROME['black'],
            highlightbackground=MONOCHROME['white'],
            highlightcolor=MONOCHROME['white'],
        )
        self.traffic_text.grid(row=1, column=1, sticky='nsew', padx=(10, 0))

    def open_settings_window(self):
        callbacks = {
            'save': self.save_settings,
            'search': self.start_auto_fetch_thread
        }
        SettingsWindow(self, self.settings, callbacks)

    def save_settings(self, new_settings):
        """保存设置回调函数"""
        self.settings.update(new_settings)
        self._apply_proxy_rotation_settings()
        self.save_settings_to_file()
        self.log("设置已保存。")
        
        if self.settings['general']['auto_retest_enabled']:
            self._start_auto_retest_timer()
        else:
            self._stop_auto_retest_timer()

        self._stop_url_health_timer(log_stop=False)
        if self.settings['general'].get('url_health_enabled'):
            self._start_url_health_timer()
        else:
            self._stop_url_health_timer()

    def load_settings_from_file(self):
        """从文件加载配置"""
        try:
            if CONFIG_PATH.exists():
                with CONFIG_PATH.open('r', encoding='utf-8') as f:
                    loaded_settings = json.load(f)
                    # Deep merge dictionaries
                    for key, value in loaded_settings.items():
                        if isinstance(value, dict) and isinstance(self.settings.get(key), dict):
                            self.settings[key].update(value)
                        else:
                            self.settings[key] = value
                self.log(f"已从 {CONFIG_PATH.name} 加载配置。")
        except Exception as e:
            self.log(f"[!] 加载配置文件失败: {e}")

    def save_settings_to_file(self):
        """保存当前配置到文件"""
        try:
            with CONFIG_PATH.open('w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
        except Exception as e:
            self.log(f"[!] 保存配置文件失败: {e}")

    def _apply_proxy_rotation_settings(self):
        general = self.settings['general']
        self.proxy_server.configure_rotation(
            request_count_enabled=general.get('request_rotation_enabled', False),
            request_count=general.get('request_rotation_count', 10),
            target_failure_enabled=general.get('target_failover_enabled', False),
            target_failure_threshold=general.get('target_failover_threshold', 3),
        )

    def _start_auto_retest_timer(self):
        self._stop_auto_retest_timer() 
        if self.rotator.get_active_proxies_count() > 0:
            interval_ms = self.settings['general']['auto_retest_interval'] * 60 * 1000
            self.log(f"自动重测已启动，间隔 {self.settings['general']['auto_retest_interval']} 分钟。")
            self.auto_retest_job_id = self.root.after(interval_ms, self._perform_auto_retest)

    def _stop_auto_retest_timer(self):
        if self.auto_retest_job_id:
            self.root.after_cancel(self.auto_retest_job_id)
            self.auto_retest_job_id = None
            self.log("自动重测已停止。")
            
    def _perform_auto_retest(self):
        if self.is_running_task or not self.settings['general']['auto_retest_enabled']:
            return

        self.log("开始执行自动重测...")
        self.start_revalidate_thread()
        
        if self.settings['general']['auto_retest_enabled']:
            interval_ms = self.settings['general']['auto_retest_interval'] * 60 * 1000
            self.auto_retest_job_id = self.root.after(interval_ms, self._perform_auto_retest)

    def _get_url_health_targets(self):
        """读取并规范化健康检查地址，支持换行、逗号或分号分隔。"""
        general_cfg = self.settings.get('general', {})
        raw_targets = general_cfg.get('url_health_urls')
        if raw_targets is None:
            raw_targets = general_cfg.get('url_health_url', '')
        if isinstance(raw_targets, (list, tuple)):
            raw_targets = '\n'.join(str(item) for item in raw_targets)

        targets = []
        for raw_target in re.split(r'[\r\n,;]+', str(raw_targets)):
            target = raw_target.strip()
            if not target:
                continue
            if '://' not in target:
                target = f'http://{target}'
            parsed = urlparse(target)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                continue
            if target not in targets:
                targets.append(target)
        return targets

    def _start_url_health_timer(self):
        """启动按设置周期执行的多地址健康检查。"""
        if self.url_health_job_id or self.url_health_running:
            return
        targets = self._get_url_health_targets()
        if not targets:
            self.log("[健康检查] 没有有效测试地址，请在设置中填写域名、IP或 http/https 地址。")
            return
        if self.rotator.get_active_proxies_count() > 0:
            interval_seconds = max(10, int(self.settings['general'].get(
                'url_health_interval_seconds', 60
            )))
            self.log(
                f"[健康检查] 已启动，每{interval_seconds}秒检查{len(targets)}个地址。"
            )
            self.url_health_job_id = self.root.after(
                interval_seconds * 1000, self._perform_url_health_check
            )

    def _stop_url_health_timer(self, log_stop=True):
        if self.url_health_job_id:
            self.root.after_cancel(self.url_health_job_id)
            self.url_health_job_id = None
        if log_stop and self.url_health_running:
            self.log("[健康检查] 已停止。")
        self.url_health_running = False

    def _perform_url_health_check(self):
        """在后台检查当前代理，避免网络操作阻塞Tk主线程。"""
        self.url_health_job_id = None
        if not self.settings['general'].get('url_health_enabled'):
            return
        targets = self._get_url_health_targets()
        if not targets:
            self.log("[健康检查] 没有有效测试地址，检查任务暂停。")
            return
        current = self.rotator.get_current_proxy()
        if not current and self.rotator.get_active_proxies_count() > 0:
            self.rotate_proxy()
            current = self.rotator.get_current_proxy()
        if current and not self.url_health_running:
            self.url_health_running = True
            proxy_snapshot = dict(current)
            threading.Thread(
                target=self._run_url_health_check,
                args=(proxy_snapshot, targets), daemon=True
            ).start()
        else:
            self._schedule_next_url_health_check()

    def _run_url_health_check(self, proxy_info, targets):
        """按顺序检查全部地址；首个失败地址触发目标级自动切换。"""
        checks = []
        failed_url = None
        failover = False
        for url in targets:
            check = self.checker.check_proxy_url(proxy_info, url)
            checks.append((url, check))
            if not check.get('ok'):
                failed_url = url
                parsed = urlparse(url)
                target_port = parsed.port or (443 if parsed.scheme == 'https' else 80)
                failover = self.proxy_server.try_domain_failover(
                    parsed.hostname, target_port, proxy_info.get('proxy')
                )
                break

        if failed_url:
            failed_check = checks[-1][1]
            result = {
                'ok': False,
                'checked': len(checks),
                'total': len(targets),
                'failed_url': failed_url,
                'error': failed_check.get('error', ''),
                'failover': failover,
            }
        else:
            result = {
                'ok': True,
                'checked': len(checks),
                'total': len(targets),
            }
        if self.root.winfo_exists():
            self.root.after(0, self._process_url_health_result, proxy_info, result)

    def _process_url_health_result(self, proxy_info, result):
        self.url_health_running = False
        proxy_address = proxy_info.get('proxy')
        if result.get('ok'):
            self.rotator.record_health_success(proxy_address)
            self.log(
                f"[健康检查] 成功: {proxy_address} "
                f"（已测试{result.get('checked', 0)}/{result.get('total', 0)}个地址）"
            )
        else:
            failure = self.rotator.record_health_failure(proxy_address, remove_after=5)
            count = failure.get('count', 0)
            reason = result.get('error') or '请求失败'
            failed_url = result.get('failed_url', '未知地址')
            if failure.get('removed'):
                self.log(
                    f"[健康检查] {failed_url} 失败 {proxy_address} "
                    f"（连续{count}/5，{reason}），已剔除。"
                )
            else:
                action = '已自动切换' if result.get('failover') else '自动轮换'
                self.log(
                    f"[健康检查] {failed_url} 失败 {proxy_address} "
                    f"（连续{count}/5，{reason}），{action}。"
                )

            if not result.get('failover'):
                self.rotate_proxy()
            self._refresh_treeview()

        self._schedule_next_url_health_check()

    def _schedule_next_url_health_check(self):
        if (self.settings['general'].get('url_health_enabled') and
                self.rotator.get_active_proxies_count() > 0 and
                not self.url_health_job_id):
            interval_seconds = max(10, int(self.settings['general'].get(
                'url_health_interval_seconds', 60
            )))
            self.url_health_job_id = self.root.after(
                interval_seconds * 1000, self._perform_url_health_check
            )
            
    def start_auto_fetch_thread(self):
        """从设置窗口启动的，仅针对空间搜索引擎的爬取任务"""
        if self._reset_ui_for_task("空间引擎搜索中..."):
            return
            
        threading.Thread(target=self.auto_fetch_and_validate, daemon=True).start()
        self.process_result_queue()

    def auto_fetch_and_validate(self):
        """执行自动爬取和验证的后台任务 (仅空间搜索引擎)"""
        self.log_queue.put("="*20 + " 步骤 1: 开始从空间搜索引擎爬取 " + "="*20)
        
        proxies_by_protocol = {'socks5': set()}

        # 1. 从 Fofa/Hunter 获取
        asset_proxies = self.asset_searcher.search_all(self.settings['auto_fetch'], self.cancel_event)
        if asset_proxies:
            proxies_by_protocol['socks5'].update(asset_proxies)

        if self.cancel_event.is_set():
            self.result_queue.put(None) 
            return

        # 转换为列表以进行验证
        final_proxies_to_validate = {
            proto: list(proxy_set) for proto, proxy_set in proxies_by_protocol.items()
        }

        self.run_validation_task(final_proxies_to_validate, 'online')


    def _run_builtin_check(self):
        proxy_str = '222.66.69.78:23344'
        self.log_queue.put(f"正在校验内置代理: http://{proxy_str}")
        builtin_proxy_info = {'proxy': proxy_str, 'protocol': 'http'}
        
        if not self.checker._pre_check_proxy(builtin_proxy_info['proxy']):
            self.log_queue.put(f"内置代理 {proxy_str} TCP 连接失败。")
            return
            
        result = self.checker._full_check_proxy(builtin_proxy_info, 'online')
        if self.root.winfo_exists():
            self.root.after(0, self._process_builtin_result, result)

    def _process_builtin_result(self, result_dict):
        if result_dict and result_dict.get('status') == 'Working':
            self._add_or_update_proxy_in_ui(result_dict)
            self.log(f"内置代理可用: {result_dict['proxy']} | 分数: {result_dict.get('score', 0):.1f}")
        elif result_dict:
            self.log(f"内置代理 {result_dict.get('proxy')} 验证失败。")
            
    def _get_quality_latency_ms(self):
        if not self.use_quality_filter_var.get():
            return None
        try:
            return int(self.quality_latency_var.get())
        except (ValueError, TclError):
            return None

    def _refresh_treeview(self, event=None):
        quality_latency = self._get_quality_latency_ms()
        self._update_regions_and_counts(quality_latency=quality_latency)
        
        selected_item = self.region_combobox.get()
        region_key = "全部国家"
        if selected_item and selected_item != "全部国家":
            match = re.match(r"(.+?)\s*\(\d+\)", selected_item)
            if match:
                region_key = match.group(1).strip()
        
        all_proxies = sorted(
            self.rotator.get_all_proxies_for_revalidation(),
            key=lambda p: (p.get('status') == 'Working', p.get('score', 0)),
            reverse=True
        )
        
        self.tree.delete(*self.tree.get_children())
        self.proxy_to_tree_item_map.clear()
        
        for p_info in all_proxies:
            region_match = (region_key == "全部国家" or p_info.get('location') == region_key)
            if not region_match:
                continue
            
            is_working = p_info.get('status') == 'Working'
            
            if self.use_quality_filter_var.get():
                if not is_working: continue
                
                latency_ms = p_info.get('latency', float('inf')) * 1000
                if quality_latency is not None and latency_ms > quality_latency:
                    continue

            score = p_info.get('score', 0)
            latency_val = p_info.get('latency', float('inf'))
            tags = () if is_working else ('unavailable',)
            
            display_values = (
                f"{score:.1f}" if is_working else "N/A", 
                p_info.get('anonymity', 'N/A'), 
                p_info.get('protocol', 'N/A'), 
                p_info.get('proxy', 'N/A'),
                f"{latency_val * 1000:.1f}" if is_working else "失效", 
                f"{p_info.get('speed', 0):.2f}" if is_working else "N/A", 
                p_info.get('location', 'N/A')
            )
            
            proxy_address = p_info.get('proxy')
            self.tree.insert('', 'end', values=display_values, tags=tags, iid=proxy_address)
            self.proxy_to_tree_item_map[proxy_address] = proxy_address
        
        if event:
            quality_str = ""
            if self.use_quality_filter_var.get():
                quality_str = f" + 优质(<{quality_latency or 'N/A'}ms)"
            self.log(f"列表已更新，显示 [{region_key}{quality_str}] 代理。")

    def process_result_queue(self):
        if not self.is_running_task:
            return

        try:
            result_dict = self.result_queue.get_nowait()
            if result_dict is None: 
                self.finalize_validation()
                return

            self.progress_bar['value'] += 1

            if result_dict.get('status') == 'Working':
                self._add_or_update_proxy_in_ui(result_dict)
                proxy_address = result_dict['proxy']
                score = result_dict.get('score', 0)
                latency = result_dict.get('latency', 0) * 1000
                self.log(f"成功: {proxy_address} | 分数: {score:.1f} | 延迟: {latency:.1f}ms")
            
            working = self.rotator.get_active_proxies_count()
            current_progress = int(self.progress_bar['value'])
            max_progress = int(self.progress_bar['maximum'])
            if max_progress > 0:
                self.log_frame.config(text=f"实时日志 | 进度: {current_progress}/{max_progress} | 可用: {working}")
            else:
                self.log_frame.config(text=f"实时日志 | 可用: {working}")

        except queue.Empty:
            pass

        if self.is_running_task:
            self.root.after(10, self.process_result_queue)

    def _add_or_update_proxy_in_ui(self, result_dict):
        proxy_address = result_dict['proxy']
        if proxy_address in self.displayed_proxies:
            self.log(f"跳过已存在代理: {proxy_address}")
            return 

        self.displayed_proxies.add(proxy_address)
        is_first_proxy = self.rotator.get_active_proxies_count() == 0
        
        latency, speed, anonymity = result_dict['latency'], result_dict['speed'], result_dict['anonymity']
        score = 0
        if latency != float('inf'): score += (1 / latency) * 50
        score += speed * 10
        if anonymity == 'Elite': score += 50
        elif anonymity == 'Anonymous': score += 20
        result_dict['score'] = score
        
        self.rotator.add_proxy(result_dict)
        
        region_key = "全部国家"
        selected_item = self.region_combobox.get()
        if selected_item and selected_item != "全部国家":
            match = re.match(r"(.+?)\s*\(\d+\)", selected_item)
            if match: region_key = match.group(1).strip()
        
        quality_latency = self._get_quality_latency_ms()
        
        region_match = (region_key == "全部国家" or result_dict.get('location') == region_key)
        quality_match = True
        if quality_latency is not None:
            quality_match = (latency * 1000 <= quality_latency)

        if region_match and quality_match:
            display_values = (
                f"{score:.1f}", anonymity, result_dict['protocol'], proxy_address,
                f"{latency * 1000:.1f}", f"{speed:.2f}", result_dict['location']
            )
            self.tree.insert('', 0, values=display_values, iid=proxy_address)
            self.sort_treeview_column('score', True)

        if is_first_proxy:
            self.log("首个可用代理已发现！功能已激活。")
        
        self._update_regions_and_counts(quality_latency=self._get_quality_latency_ms())
        working = self.rotator.get_active_proxies_count()
        self.log_frame.config(text=f"实时日志 | 可用: {working}")

    def _update_regions_and_counts(self, quality_latency=None):
        working_count = self.rotator.get_active_proxies_count()
        total_count = len(self.rotator.get_all_proxies_for_revalidation())
        
        if not self.is_running_task:
            try:
                self.log_frame.config(text=f"实时日志 | 可用: {working_count} / 总计: {total_count}")
            except (AttributeError, TclError):
                pass

        regions_with_counts = self.rotator.get_available_regions_with_counts(quality_latency_ms=quality_latency)
        current_selection = self.region_combobox.get()
        
        if regions_with_counts:
            sorted_regions = sorted(regions_with_counts.items(), key=lambda item: item[1], reverse=True)
            formatted_regions = [f"{region} ({count})" for region, count in sorted_regions]
            
            new_values = ["全部国家"] + formatted_regions
            
            current_region_key = None
            if current_selection and current_selection != "全部国家":
                match = re.match(r"(.+?)\s*\(\d+\)", current_selection)
                if match:
                    current_region_key = match.group(1).strip()

            self.region_combobox['values'] = new_values
            
            new_selection_found = False
            if current_region_key:
                for item in new_values:
                    if item.startswith(current_region_key):
                        self.region_combobox.set(item)
                        new_selection_found = True
                        break
            
            if not new_selection_found:
                self.region_combobox.set("全部国家")
        else:
            self.region_combobox['values'] = ["全部国家"]
            self.region_combobox.set("全部国家")
        
        if total_count > 0:
            self.test_all_button.config(state=tk.NORMAL)
        else:
            self.test_all_button.config(state=tk.DISABLED)

        if working_count > 0:
            self.export_button.config(state=tk.NORMAL)
            self.server_button.config(state=tk.NORMAL)
            self.rotate_button.config(state=tk.NORMAL)
            self.auto_rotate_button.config(state=tk.NORMAL)
            if self.settings['general']['auto_retest_enabled']: self._start_auto_retest_timer()
            if self.settings['general'].get('url_health_enabled'): self._start_url_health_timer()
        else:
            self.export_button.config(state=tk.DISABLED)
            self.server_button.config(state=tk.DISABLED)
            self.rotate_button.config(state=tk.DISABLED)
            self.auto_rotate_button.config(state=tk.DISABLED)
            self.current_proxy_var.set("当前使用: N/A")
            if self.is_server_running: self.toggle_server()
            if self.is_auto_rotating: self.toggle_auto_rotate()
            self._stop_auto_retest_timer()
            self._stop_url_health_timer()

    def finalize_validation(self):
        self.is_running_task = False
        self.fetch_button.config(state=tk.NORMAL)
        self.import_button.config(state=tk.NORMAL)
        self.clear_button.config(state=tk.NORMAL)
        self.cancel_button.config(state=tk.DISABLED, text="取消任务")
        self.settings_button.config(state=tk.NORMAL)
        
        self._refresh_treeview() 
        
        final_count = self.rotator.get_active_proxies_count()
        total_count = len(self.rotator.get_all_proxies_for_revalidation())
        self.log_frame.config(text=f"实时日志 | 可用: {final_count} / 总计: {total_count}")
        self.log(f"\n{'='*20} 任务全部完成 {'='*20}\n代理池中现有 {final_count} 个可用的代理。")

    def finalize_revalidation(self):
        self.is_running_task = False
        self.fetch_button.config(state=tk.NORMAL)
        self.import_button.config(state=tk.NORMAL)
        self.clear_button.config(state=tk.NORMAL)
        self.test_all_button.config(text="全部重测")
        self.cancel_button.config(state=tk.DISABLED, text="取消任务")
        self.settings_button.config(state=tk.NORMAL)

        self._refresh_treeview()
        self.sort_treeview_column('score', True)

        final_count = self.rotator.get_active_proxies_count()
        total_count = len(self.rotator.get_all_proxies_for_revalidation())
        self.log_frame.config(text=f"实时日志 | 可用: {final_count} / 总计: {total_count}")
        self.log(f"\n{'='*20} 全部重测完成 {'='*20}\n代理池中现有 {final_count} 个可用的代理。")
        self.proxy_to_tree_item_map.clear()
        
    def finalize_task_cancellation(self):
        self.is_running_task = False
        while not self.result_queue.empty():
            try:
                self.result_queue.get_nowait()
            except queue.Empty:
                break
        
        self.fetch_button.config(state=tk.NORMAL)
        self.import_button.config(state=tk.NORMAL)
        self.clear_button.config(state=tk.NORMAL)
        self.test_all_button.config(state=tk.NORMAL)
        self.cancel_button.config(state=tk.DISABLED, text="取消任务")
        self.settings_button.config(state=tk.NORMAL)
        
        self._update_regions_and_counts(quality_latency=self._get_quality_latency_ms())
        self.log("\n" + "="*20 + " 任务已被用户强制取消 " + "="*20)

    def _delete_selected_proxy(self):
        selected_items = self.tree.selection()
        if not selected_items:
            return
        
        item_id = selected_items[0]
        proxy_address = self.tree.item(item_id, 'values')[3]
        
        if self.rotator.remove_proxy(proxy_address):
            if proxy_address in self.displayed_proxies:
                self.displayed_proxies.remove(proxy_address)
            
            self.log(f"已手动删除代理: {proxy_address}")
            self._refresh_treeview()
        else:
            self.log(f"错误: 尝试删除的代理 {proxy_address} 在后端未找到。")

    def rotate_proxy(self):
        selected_item = self.region_combobox.get()
        region_key = "All"
        display_region = "全部国家"
        
        if selected_item and selected_item != "全部国家":
            match = re.match(r"(.+?)\s*\(\d+\)", selected_item)
            if match:
                region_key = match.group(1).strip()
                display_region = region_key
    
        quality_latency = self._get_quality_latency_ms()
        
        self.rotator.set_filters(region=region_key, quality_latency_ms=quality_latency)
        proxy_info = self.rotator.get_next_proxy()
        
        mode_str = f"优质(<{quality_latency}ms)" if quality_latency is not None else "常规"
        
        if proxy_info:
            if not (self.is_auto_rotating and self.interval_spinbox.get() == "0"):
                 self.current_proxy_var.set(f"当前使用: {proxy_info['proxy']}")
            self.log(f"已轮换代理 ({display_region} | {mode_str}模式): {proxy_info['protocol'].lower()}://{proxy_info['proxy']}")
        else:
            self.current_proxy_var.set("当前使用: N/A")
            self.log(f"[{display_region}] 内无可用({mode_str}模式)代理。")

    def log(self, message):
        if not hasattr(self, 'log_text') or not self.root.winfo_exists(): 
            print(f"LOG: {message}") # Fallback to console if GUI not ready
            return
        self.log_text.config(state='normal')
        self.log_text.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')

    def log_traffic(self, message):
        if not hasattr(self, 'traffic_text') or not self.root.winfo_exists():
            print(f"TRAFFIC: {message}")
            return
        self.traffic_text.config(state='normal')
        self.traffic_text.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {message}\n")
        self.traffic_text.see(tk.END)
        self.traffic_text.config(state='disabled')

    def clear_all_proxies(self):
        if self.is_running_task:
            messagebox.showwarning("操作无效", "请等待当前任务完成后再清空列表。")
            return
        if messagebox.askyesno("确认操作", "您确定要清空所有代理吗？此操作不可逆。"):
            self.log("正在清空所有代理...")
            self.rotator.clear()
            self.displayed_proxies.clear()
            self._stop_auto_retest_timer()
            self._stop_url_health_timer()
            self.log("所有代理已清空。")
            self._refresh_treeview()

    def _reset_ui_for_task(self, task_name="正在运行..."):
        if self.is_running_task: return True
        self.is_running_task = True
        self.cancel_event.clear()
        
        self.fetch_button.config(state=tk.DISABLED)
        self.import_button.config(state=tk.DISABLED)
        self.clear_button.config(state=tk.DISABLED)
        self.test_all_button.config(state=tk.DISABLED)
        self.export_button.config(state=tk.DISABLED)
        self.settings_button.config(state=tk.DISABLED)
        self.cancel_button.config(state=tk.NORMAL, text=f"取消{task_name.replace('...','').replace('正在','')}")
        
        self.progress_bar['value'] = 0
        return False
        
    def cancel_current_task(self):
        if self.is_running_task:
            self.log("正在发送取消信号... UI已解锁，后台任务将尽快终止。")
            self.cancel_event.set()
            self.finalize_task_cancellation()

    def start_fetch_validate_thread(self):
        if self._reset_ui_for_task("获取中..."): return
        threading.Thread(target=self.fetch_and_validate, daemon=True).start()
        self.process_result_queue()

    def import_and_validate_proxies(self):
        file_path = filedialog.askopenfilename(
            title="导入代理(TXT/JSON)",
            filetypes=[("Text and JSON files", "*.txt *.json"), ("All files", "*.*")]
        )
        if not file_path: return
        proxies_by_protocol = {'http': [], 'socks4': [], 'socks5': []}
        valid_parse_protocols = {'http', 'https', 'socks4', 'socks5'}
        try:
            _, ext = os.path.splitext(file_path)
            if ext.lower() == '.json':
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        for item in data:
                            url, protocol = item.get('url'), item.get('protocol', 'http').lower()
                            if url:
                                parsed = re.match(r'(\w+)://(.+)', url)
                                if parsed: protocol, proxy = parsed.groups()
                                else: proxy = url
                            else: proxy = f"{item.get('ip')}:{item.get('port')}"
                            if protocol == 'https': protocol = 'http'
                            if protocol in proxies_by_protocol: proxies_by_protocol[protocol].append(proxy)
            else: 
                with open(file_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'): continue
                        protocol, proxy_address = 'http', line
                        match = re.match(r'(\w+)://(.+)', line)
                        if match:
                            proto_part, proxy_part = match.groups()
                            if proto_part.lower() in valid_parse_protocols:
                                proxy_address = proxy_part
                                protocol = 'http' if proto_part.lower() == 'https' else proto_part.lower()
                        elif ',' in line:
                            parts = [p.strip().lower() for p in line.split(',', 1)]
                            if len(parts) == 2 and parts[0] in valid_parse_protocols:
                                proxy_address, protocol = parts[1], 'http' if parts[0] == 'https' else parts[0]
                        if protocol in proxies_by_protocol and re.match(r'^\d{1,3}(?:\.\d{1,3}){3}:\d+$', proxy_address):
                             proxies_by_protocol[protocol].append(proxy_address)
                        else: self.log(f"已跳过无效格式行: {line}")
            total_imported = sum(len(v) for v in proxies_by_protocol.values())
            if total_imported == 0:
                messagebox.showwarning("无内容", "文件中未找到有效格式的代理。")
                return
            self.log(f"成功从文件导入 {total_imported} 个代理，准备验证...")
            if self._reset_ui_for_task("验证中..."): return
            threading.Thread(target=self.run_validation_task, args=(proxies_by_protocol, 'import'), daemon=True).start()
            self.process_result_queue()
        except Exception as e:
            messagebox.showerror("导入错误", f"读取或解析文件时出错: {e}")
            self.log(f"导入代理失败: {e}")
            self.finalize_validation()

    def fetch_and_validate(self):
        self.log_queue.put("="*20 + " 步骤 1: 开始获取在线免费代理 " + "="*20)
        proxies_by_protocol = self.fetcher.fetch_all(self.log_queue, cancel_event=self.cancel_event)

        if self.cancel_event.is_set():
            self.result_queue.put(None) 
            return

        self.run_validation_task(proxies_by_protocol, validation_mode='online')

    def run_validation_task(self, proxies_by_protocol, validation_mode='online'):
        total_to_validate = sum(len(v) for v in proxies_by_protocol.values())
        if self.root.winfo_exists(): self.root.after(0, self.progress_bar.config, {'maximum': total_to_validate})
        if total_to_validate > 0:
            self.checker.validate_all(
                proxies_by_protocol, self.result_queue, self.log_queue, validation_mode,
                max_workers=self.settings['general']['validation_threads'],
                cancel_event=self.cancel_event
            )
        else:
            self.result_queue.put(None) 

    def process_log_queue(self):
        try:
            while True: self.log(self.log_queue.get_nowait())
        except queue.Empty: pass
        try:
            while True: self.log_traffic(self.traffic_queue.get_nowait())
        except queue.Empty: pass
        if self.root.winfo_exists(): self.root.after(100, self.process_log_queue)

    def start_revalidate_thread(self):
        if self._reset_ui_for_task("重测中..."): return
        self.test_all_button.config(text="重测中...")
        threading.Thread(target=self.revalidate_all, daemon=True).start()
        self.process_revalidate_queue()

    def revalidate_all(self):
        self.log_queue.put("="*20 + " 开始重新验证所有代理 (按分数优先) " + "="*20)
        all_current_proxies_info = self.rotator.get_all_proxies_for_revalidation()

        if not all_current_proxies_info:
            self.log_queue.put("代理池为空，无需测试。")
            self.result_queue.put(None)
            return
        
        all_current_proxies_info.sort(key=lambda p: p.get('score', -1), reverse=True)
        
        from collections import defaultdict
        proxies_by_protocol = defaultdict(list)
        for p_info in all_current_proxies_info:
            protocol = p_info.get('protocol', 'http').lower()
            proxy = p_info.get('proxy')
            if proxy:
                proxies_by_protocol[protocol].append(proxy)
        self.run_validation_task(proxies_by_protocol, 'online')

    def process_revalidate_queue(self):
        if not self.is_running_task:
            return
            
        try:
            result_dict = self.result_queue.get_nowait()
            if result_dict is None: 
                self.finalize_revalidation()
                return

            self.progress_bar['value'] += 1
            proxy_address = result_dict['proxy']
            
            original_proxy_info = self.rotator.get_proxy_by_address(proxy_address)
            if not original_proxy_info:
                # This can happen if the proxy was removed during revalidation
                # self.log(f"更新跳过: 代理 {proxy_address} 在测试完成时已不存在。")
                return

            tree_item_id = proxy_address
            
            if result_dict.get('status') == 'Working':
                latency, speed, anonymity = result_dict['latency'], result_dict['speed'], result_dict['anonymity']
                score = 0
                if latency != float('inf'): score += (1 / latency) * 50
                score += speed * 10
                if anonymity == 'Elite': score += 50
                elif anonymity == 'Anonymous': score += 20
                
                update_data = {
                    'score': score, 'status': 'Working', 'consecutive_failures': 0,
                    'latency': latency, 'speed': speed, 'anonymity': anonymity,
                    'location': result_dict['location']
                }
                self.rotator.update_proxy(proxy_address, update_data)

                if self.tree.exists(tree_item_id):
                    display_values = (
                        f"{score:.1f}", anonymity, result_dict['protocol'], proxy_address,
                        f"{latency * 1000:.1f}", f"{speed:.2f}", result_dict['location']
                    )
                    self.tree.item(tree_item_id, values=display_values, tags=())
                self.log(f"更新: {proxy_address} | 分数: {score:.1f} | 延迟: {latency*1000:.1f}ms")
            else: 
                new_failures = original_proxy_info.get('consecutive_failures', 0) + 1
                
                if new_failures >= self.settings['general']['failure_threshold']:
                    self.log(f"测试失败超阈值({self.settings['general']['failure_threshold']}次)，正在移除: {proxy_address}")
                    if self.rotator.remove_proxy(proxy_address):
                        if proxy_address in self.displayed_proxies:
                            self.displayed_proxies.remove(proxy_address)
                        if self.tree.exists(tree_item_id):
                            self.tree.delete(tree_item_id)
                else:
                    self.log(f"测试失败: {proxy_address} (第 {new_failures} 次)")
                    update_data = {'status': 'Unavailable', 'consecutive_failures': new_failures}
                    self.rotator.update_proxy(proxy_address, update_data)
                    if self.tree.exists(tree_item_id):
                        values = list(self.tree.item(tree_item_id, 'values'))
                        values[0] = "N/A"
                        values[4] = "失效"
                        values[5] = "N/A"
                        self.tree.item(tree_item_id, values=values, tags=('unavailable',))

            working = self.rotator.get_active_proxies_count()
            current_progress = int(self.progress_bar['value'])
            max_progress = int(self.progress_bar['maximum'])
            if max_progress > 0:
                self.log_frame.config(text=f"实时日志 | 进度: {current_progress}/{max_progress} | 可用: {working}")
            else:
                self.log_frame.config(text=f"实时日志 | 可用: {working}")

        except queue.Empty:
            pass
        
        if self.is_running_task:
            self.root.after(20, self.process_revalidate_queue)

    def sort_treeview_column(self, col, reverse):
        data = [(self.tree.set(child, col), child) for child in self.tree.get_children('')]
        try:
            # Helper function to convert to float, falling back for non-numeric data
            def sort_key(t):
                val_str = t[0]
                try:
                    return float(val_str)
                except ValueError:
                    # Place non-numeric/failed items at the end when sorting descending, start for ascending
                    return float('-inf') if reverse else float('inf') 
            data.sort(key=sort_key, reverse=reverse)
        except ValueError: # Fallback for completely non-numeric columns
            data.sort(key=lambda t: str(t[0]), reverse=reverse)
        for index, (val, child) in enumerate(data):
            self.tree.move(child, '', index)

    def copy_to_clipboard(self, event):
        selected_item = self.tree.selection()
        if not selected_item: return
        proxy_address = self.tree.item(selected_item[0], 'values')[3]
        self.root.clipboard_clear(); self.root.clipboard_append(proxy_address)
        self.log(f"已复制到剪贴板: {proxy_address}")
        
    def export_proxies(self):
        working_proxies = [p for p in self.rotator.get_all_proxies_for_revalidation() if p.get('status') == 'Working']
        if not working_proxies:
            messagebox.showwarning("无内容", "没有可用的代理可以导出。")
            return
        
        file_path = filedialog.asksaveasfilename(title="导出可用代理到文件", defaultextension=".txt", filetypes=[("Text files", "*.txt"), ("CSV files", "*.csv"), ("JSON files", "*.json")])
        if not file_path: return
        try:
            _, ext = os.path.splitext(file_path)
            if ext.lower() == '.json':
                with open(file_path, 'w', encoding='utf-8') as f:
                    export_data = [{'protocol': p['protocol'], 'proxy': p['proxy'], 'location': p['location']} for p in working_proxies]
                    json.dump(export_data, f, indent=2, ensure_ascii=False)
            elif ext.lower() == '.csv':
                with open(file_path, 'w', encoding='utf-8', newline='') as f:
                    f.write("score,anonymity,protocol,proxy,latency_ms,speed_mbps,location\n")
                    for p in working_proxies:
                        lat_ms, spd_mbps = f"{p['latency'] * 1000:.1f}", f"{p['speed']:.2f}"
                        score = p.get('score', 0)
                        f.write(f"{score:.1f},{p['anonymity']},{p['protocol']},{p['proxy']},{lat_ms},{spd_mbps},\"{p['location']}\"\n")
            else: # Default to TXT
                 with open(file_path, 'w', encoding='utf-8') as f:
                    for p in working_proxies: f.write(f"{p['protocol'].lower()}://{p['proxy']}\n")
            
            self.log(f"成功导出 {len(working_proxies)} 个代理到 {file_path}")
            messagebox.showinfo("成功", f"已成功导出 {len(working_proxies)} 个代理。")
        except Exception as e:
            self.log(f"导出代理失败: {e}")
            messagebox.showerror("失败", f"导出代理时发生错误:\n{e}")

    def _show_context_menu(self, event):
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        self.tree.selection_set(item_id)
        
        proxy_info = self.rotator.get_proxy_by_address(item_id)
        if not proxy_info: return

        context_menu = tk.Menu(
            self.root, tearoff=0,
            background=MONOCHROME['black'], foreground=MONOCHROME['white'],
            activebackground=MONOCHROME['white'], activeforeground=MONOCHROME['black'],
            borderwidth=1, relief=tk.SOLID,
        )
        if proxy_info.get('status') == 'Working':
            context_menu.add_command(label="使用此代理", command=self._use_selected_proxy)
        context_menu.add_command(label="删除此代理", command=self._delete_selected_proxy)
        context_menu.tk_popup(event.x_root, event.y_root)

    def _use_selected_proxy(self):
        selected_items = self.tree.selection()
        if not selected_items:
            return
        proxy_address = self.tree.item(selected_items[0], 'values')[3]
        proxy_info = self.rotator.set_current_proxy_by_address(proxy_address)
        if proxy_info:
            self.current_proxy_var.set(f"当前使用: {proxy_info['proxy']}")
            self.log(f"已手动切换代理: {proxy_info['protocol'].lower()}://{proxy_info['proxy']}")
        else:
            self.log(f"错误: 尝试设置的代理 {proxy_address} 在轮换器中未找到或不可用。")
            
    def toggle_server(self):
        if self.is_server_running:
            self.proxy_server.stop_all()
            self.server_button.config(text="启动服务", style='info.TButton')
            self.is_server_running = False
        else:
            if self.rotator.get_active_proxies_count() == 0:
                messagebox.showwarning("启动失败", "代理池中无可用代理，无法启动服务。")
                return
            if not self.rotator.get_current_proxy(): self.rotate_proxy()
            if not self.proxy_server.start_all():
                messagebox.showerror("启动失败", "本地 HTTP 或 SOCKS5 监听端口无法启动，请检查端口是否被占用。")
                return
            self.server_button.config(text="停止服务", style='danger.TButton')
            self.is_server_running = True

    def _on_closing(self):
        if self.is_server_running: self.proxy_server.stop_all()
        self._stop_auto_retest_timer()
        self._stop_url_health_timer()
        self._stop_auto_rotate_timer()
        self.save_settings_to_file()
        self.root.destroy()
        
    def _stop_auto_rotate_timer(self):
        if self.auto_rotate_job_id:
            self.root.after_cancel(self.auto_rotate_job_id)
            self.auto_rotate_job_id = None
        
    def toggle_auto_rotate(self):
        if self.is_auto_rotating:
            self.is_auto_rotating = False
            self._stop_auto_rotate_timer()
            self.auto_rotate_button.config(text="自动", style='info.TButton')
            self.log("自动轮换已停止。")
            current_p = self.rotator.get_current_proxy()
            if current_p:
                self.current_proxy_var.set(f"当前使用: {current_p['proxy']}")
            else:
                self.current_proxy_var.set("当前使用: N/A")
        else:
            try:
                interval_sec = int(self.interval_spinbox.get())
                if interval_sec <= 0: raise ValueError()
            except ValueError:
                messagebox.showerror("无效间隔", "时间间隔必须是大于 0 的整数。")
                return

            if self.rotator.get_active_proxies_count() == 0:
                messagebox.showwarning("启动失败", "代理池中无可用代理，无法启动自动轮换。")
                return
                
            self.is_auto_rotating = True
            self.auto_rotate_button.config(text="停止", style='danger.TButton')
            
            self.rotate_proxy()

            self.log(f"自动轮换已启动，间隔 {interval_sec} 秒。")
            self._perform_auto_rotation()
            
    def _perform_auto_rotation(self):
        if not self.is_auto_rotating: return
        self.rotate_proxy()
        try:
            interval_ms = int(self.interval_spinbox.get()) * 1000
            if interval_ms > 0:
                self.auto_rotate_job_id = self.root.after(interval_ms, self._perform_auto_rotation)
        except (ValueError, TclError): 
            if self.is_auto_rotating: self.toggle_auto_rotate()

if __name__ == "__main__":
    # 确保在Windows上获得更清晰的字体渲染
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = bs.Window(themename="superhero")
    configure_monochrome_theme(root)
    app = ProxyPoolApp(root)
    root.mainloop()
