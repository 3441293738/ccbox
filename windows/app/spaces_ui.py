"""Account cards, with read-only background status and explicit per-space actions."""
import threading
import time

from . import spaces
from .ui_components import COLORS as C, text, wrapped


class SpacesUI:
    def spaces_page(self):
        self.space_cards = {}
        self.space_refreshing = False
        self.space_destroyed = False
        self.space_refresh_at = 0
        page = self.pages['账号空间']
        bar = self.frame(page)
        bar.pack(fill='x', pady=(0, 12))
        self.space_summary = text(bar, '正在读取空间…', size=13, color=C['muted'])
        self.space_summary.pack(side='left', fill='x', expand=True)
        self.button(bar, '刷新状态', lambda:self.refresh_spaces(force=True), track=False, width=100).pack(side='right')
        self.button(bar, '+ 新建账号空间', self.create_environment, width=150).pack(side='right', padx=8)
        self.space_list = self.frame(page)
        self.space_list.pack(fill='x')
        note = self.card(page, '每个账号，一套独立资料')
        wrapped(note, '登录、代理、浏览器和桌面按空间分别保存。切换只改变当前操作对象，其他沙箱继续运行。', size=13).pack(fill='x', pady=(8, 6))
        wrapped(note, '空间名称是你填写的备注，不代表已核验 Claude 登录账号。同一项目若共享给多个空间，文件修改彼此可见；建议使用独立目录或 Git worktree。', size=12).pack(fill='x')
        wrapped(note, '多个沙箱会同时占用 CPU 和内存。不用的空间可单独停止，登录资料保留。本机开发通道仍在切换时断开，避免命令执行到错误账号或项目。', size=12).pack(fill='x', pady=(8, 0))
        self.render_spaces(spaces.catalog_rows(self.c.root))

    def render_spaces(self, rows):
        keys = [row['id'] for row in rows]
        if list(self.space_cards) != keys:
            for child in self.space_list.winfo_children():
                child.destroy()
            self.space_cards = {}
            for row in rows:
                key = row['id']
                outer = self.frame(self.space_list, C['card'], corner_radius=12, border_width=1, border_color=C['divider'])
                outer.pack(fill='x', pady=(0, 12))
                body = self.frame(outer)
                body.pack(fill='x', padx=20, pady=18)
                top = self.frame(body)
                top.pack(fill='x')
                status = text(top, '', size=13, weight='bold')
                status.pack(side='right', padx=(12, 0))
                name = wrapped(top, '', size=16, weight='bold', color=C['text'])
                name.pack(side='left', fill='x', expand=True)
                meta = wrapped(body, '', size=12)
                meta.pack(fill='x', pady=(7, 4))
                detail = wrapped(body, '', size=12, color=C['subtle'])
                detail.pack(fill='x')
                actions = self.action_row(body)
                open_button = self.button(actions, '启动并打开', lambda k=key:self.space_action(k, 'open'), True, track=False, width=160)
                open_button.pack(side='left', padx=(0, 8))
                settings = self.button(actions, '设置 / 项目', lambda k=key:self.space_action(k, 'settings'), track=False, width=120)
                settings.pack(side='left', padx=(0, 8))
                stop = self.button(actions, '停止', lambda k=key:self.space_action(k, 'stop'), track=False, width=80)
                stop.pack(side='left')
                self.space_cards[key] = dict(outer=outer, name=name, status=status, meta=meta, detail=detail,
                                             open=open_button, settings=settings, stop=stop, row=row)
        running = sum(row.get('state') == 'running' for row in rows)
        unknown = sum(row.get('state', 'unknown') == 'unknown' for row in rows)
        self.space_summary.configure(text=f'{len(rows)} 个空间 · {running} 个运行中' + (' · 状态待确认' if unknown else ''))
        for row in rows:
            card = self.space_cards[row['id']]
            card['row'] = row
            selected = row['id'] == self.c.environment_id
            card['outer'].configure(border_color=C['accent'] if selected else C['divider'], border_width=2 if selected else 1)
            card['name'].configure(text=row['name'] + ('  · 当前' if selected else ''))
            state = row.get('state', 'unknown')
            card['status'].configure(text=row.get('status', '等待刷新'), text_color=C['accent'] if state == 'running' else C['danger'] if state == 'error' else C['muted'])
            card['meta'].configure(text=row['summary'] + (f"  |  桌面端口 {row['port']}" if row['port'] else ''))
            card['detail'].configure(text=row.get('detail', '只读取状态，不自动启动或停止。'))
            card['open'].configure(text='打开 VS Code' if state == 'running' else '启动并打开')
        self.spaces_busy_state()

    def spaces_busy_state(self):
        for card in getattr(self, 'space_cards', {}).values():
            row = card['row']
            card['open'].configure(state='disabled' if self.busy or row.get('invalid') else 'normal')
            card['settings'].configure(state='disabled' if self.busy else 'normal')
            card['stop'].configure(state='disabled' if self.busy or row.get('state') not in ('running', 'partial', 'error') or not row.get('project') or row.get('invalid') else 'normal')

    def refresh_spaces(self, force=False):
        if not self.monitor_spaces or self.space_destroyed or self.space_refreshing or (not force and time.monotonic() < self.space_refresh_at):
            return
        controller = self.c  # Capture identity; worker never reads mutable UI state.
        root = str(controller.root)
        data_root = str(controller.data_root)
        self.space_refreshing = True
        self.space_refresh_at = time.monotonic() + 5
        def worker():
            try:
                rows = spaces.snapshot(controller.root, controller.run, controller.docker)
                error = False
            except Exception:
                rows, error = [], True
            self.messages.put(('spaces_status', (root, data_root, rows, error)))
        threading.Thread(target=worker, daemon=True).start()

    def spaces_received(self, value):
        self.space_refreshing = False
        root, data_root, rows, error = value
        if root != str(self.c.root) or data_root != str(self.c.data_root):
            return
        if error:
            self.space_summary.configure(text='读取空间状态失败；请检查目录或刷新重试。')
            return
        # A catalog may change while Docker is responding. Never resurrect old cards.
        try:
            catalog = spaces.catalog_rows(self.c.root)
        except Exception:
            self.space_summary.configure(text='空间目录读取失败；原容器未改动。')
            return
        if [(r['id'], r['name']) for r in rows] != [(r['id'], r['name']) for r in catalog]:
            self.space_refresh_at = 0
            return
        self.render_spaces(rows)

    def space_action(self, key, action):
        if self.busy:
            self.notify('正在处理一个空间；完成后可操作其他空间。已有容器继续运行。')
            return
        if key != self.c.environment_id:
            if not self.can_change_environment():
                return
            try:
                self.load_environment(key)
            except Exception as error:
                self.notify(str(error), error=True)
                return
        if action == 'open':
            self.show('概览')
            self.start_development()
        elif action == 'settings':
            self.show('概览')
        elif action == 'stop':
            self.stop_environment()

    def spaces_selection_changed(self):
        if hasattr(self, 'space_cards'):
            cached = {key:card['row'] for key, card in self.space_cards.items()}
            rows = [{**cached.get(row['id'], {}), **row} for row in spaces.catalog_rows(self.c.root)]
            self.render_spaces(rows)
            self.space_refresh_at = 0
